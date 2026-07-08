"""
Unit tests for apps.live_trader.

These are app-wiring tests: they confirm the live trader loads
credentials, invokes the broker factory with the right config, builds the
same core pipeline the paper trader uses, and prints a sane startup
banner - without ever performing real network I/O against a real Groww
endpoint. One test (see
``test_run_live_session_fails_fast_without_a_real_endpoint``) does
exercise the real, unmocked Groww transport end-to-end; it is safe
because an empty/placeholder base_url fails at URL-parsing time, before
any socket or DNS activity - see ``test_groww_transport.py`` for the
transport-level version of the same guarantee.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_live_trader.py -v
"""
from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from apps.live_trader.config import (
    LIVE_CONFIRM_REQUIRED_VALUE,
    LiveTradingNotConfirmedError,
    MissingGrowwCredentialsError,
    load_groww_credentials_from_env,
)
from apps.live_trader.main import (
    LiveTransportNotReadyError,
    _ensure_live_transport_ready,
    build_live_session,
    print_startup_banner,
    run_live_session,
)
from core.audit.models import AuditEvent, SessionMetadata, SessionSummary
from core.audit.sink import AuditSink, NullAuditSink
from core.broker.factory_models import BrokerConfig
from core.broker.groww_adapter import GrowwBrokerAdapter
from core.broker.groww_models import GrowwCredentials
from core.broker.paper import PaperBroker
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.errors import MarketDataTransportError
from core.market_data.groww_live_feed import GrowwLiveMarketDataFeed
from core.market_data.memory_feed import MemoryMarketDataFeed

VALID_ENV = {
    "GROWW_API_KEY": "test-api-key",
    "GROWW_ACCESS_TOKEN": "test-access-token",
}

# Env for the real Groww live broker path: requires both credentials and
# the explicit safety-gate confirmation string. Market data must also be
# "groww" - "groww broker + memory data" is not a supported combination
# (see apps/live_trader/config.py's _ALLOWED_PROVIDER_COMBINATIONS).
GROWW_LIVE_BROKER_ENV = {
    **VALID_ENV,
    "LIVE_BROKER_PROVIDER": "groww",
    "LIVE_MARKET_DATA_PROVIDER": "groww",
    "LIVE_CONFIRM": LIVE_CONFIRM_REQUIRED_VALUE,
}


class RecordingAuditSink(AuditSink):
    """In-memory AuditSink test double: no file I/O, just records calls."""

    def __init__(self) -> None:
        self.started_with: SessionMetadata | None = None
        self.events: list[AuditEvent] = []
        self.finalized_with: SessionSummary | None = None
        self.failed_with: BaseException | None = None

    def start_session(self, metadata: SessionMetadata) -> None:
        self.started_with = metadata

    def record_event(self, event: AuditEvent) -> None:
        self.events.append(event)

    def finalize_session(self, summary: SessionSummary) -> None:
        self.finalized_with = summary

    def record_failure(self, error: BaseException) -> None:
        self.failed_with = error


class LoadGrowwCredentialsFromEnvTests(unittest.TestCase):
    @patch.dict(os.environ, {"GROWW_API_KEY": "", "GROWW_ACCESS_TOKEN": ""})
    def test_missing_both_credentials_raises(self) -> None:
        with self.assertRaises(MissingGrowwCredentialsError) as ctx:
            load_groww_credentials_from_env()

        self.assertIn("GROWW_API_KEY", str(ctx.exception))
        self.assertIn("GROWW_ACCESS_TOKEN", str(ctx.exception))

    @patch.dict(os.environ, {"GROWW_API_KEY": "some-key", "GROWW_ACCESS_TOKEN": ""})
    def test_missing_access_token_raises(self) -> None:
        with self.assertRaises(MissingGrowwCredentialsError) as ctx:
            load_groww_credentials_from_env()

        self.assertIn("GROWW_ACCESS_TOKEN", str(ctx.exception))
        self.assertNotIn("GROWW_API_KEY,", str(ctx.exception))

    @patch.dict(os.environ, {**VALID_ENV, "GROWW_BASE_URL": "https://api.groww.example"})
    def test_valid_credentials_are_loaded(self) -> None:
        credentials = load_groww_credentials_from_env()

        self.assertEqual(credentials.api_key, "test-api-key")
        self.assertEqual(credentials.access_token, "test-access-token")
        self.assertEqual(credentials.base_url, "https://api.groww.example")

    @patch.dict(os.environ, VALID_ENV)
    def test_base_url_optional_defaults_to_empty(self) -> None:
        os.environ.pop("GROWW_BASE_URL", None)

        credentials = load_groww_credentials_from_env()

        self.assertEqual(credentials.base_url, "")


class EnsureLiveTransportReadyTests(unittest.TestCase):
    def test_real_groww_live_adapter_is_ready_now_that_transport_is_wired(self) -> None:
        # As of Groww HTTP Transport V1, GrowwBrokerAdapter.LIVE_TRANSPORT_IMPLEMENTED
        # is True - the outbound seams call a real (if unverified-against-
        # real-Groww-docs) HTTP transport instead of raising NotImplementedError.
        adapter = GrowwBrokerAdapter(
            credentials=GrowwCredentials(api_key="k", access_token="t"),
            mode="live",
        )

        _ensure_live_transport_ready(adapter)  # must not raise

    def test_gate_still_blocks_when_transport_flag_is_false(self) -> None:
        # Proves the gate *mechanism* itself still works, independent of
        # Groww's current implemented/not-implemented state - e.g. if the
        # flag were ever reverted, or a future broker reuses this pattern.
        adapter = GrowwBrokerAdapter(
            credentials=GrowwCredentials(api_key="k", access_token="t"),
            mode="live",
        )

        with patch.object(GrowwBrokerAdapter, "LIVE_TRANSPORT_IMPLEMENTED", False):
            with self.assertRaises(LiveTransportNotReadyError):
                _ensure_live_transport_ready(adapter)

    def test_groww_paper_mode_is_not_gated(self) -> None:
        adapter = GrowwBrokerAdapter(
            credentials=GrowwCredentials(api_key="k", access_token="t"),
            mode="paper",
        )

        _ensure_live_transport_ready(adapter)  # must not raise

    def test_non_groww_broker_is_not_gated(self) -> None:
        _ensure_live_transport_ready(PaperBroker())  # must not raise


class BuildLiveSessionTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=False)
    def test_paper_broker_and_memory_data_needs_no_credentials(self) -> None:
        # The safe default: LIVE_BROKER_PROVIDER/LIVE_MARKET_DATA_PROVIDER
        # both unset -> paper + memory, which must build successfully with
        # zero Groww credentials in the environment at all.
        for var in ("GROWW_API_KEY", "GROWW_ACCESS_TOKEN", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        ctx = build_live_session()

        self.assertIsInstance(ctx.broker, PaperBroker)
        self.assertEqual(ctx.broker_config.provider, "paper")
        self.assertEqual(ctx.market_data_config.provider, "memory")

    @patch.dict(
        os.environ,
        {**VALID_ENV, "LIVE_BROKER_PROVIDER": "groww", "LIVE_MARKET_DATA_PROVIDER": "groww"},
        clear=False,
    )
    def test_groww_live_broker_without_confirmation_is_rejected(self) -> None:
        os.environ.pop("LIVE_CONFIRM", None)

        with patch("apps.live_trader.main.build_broker") as mock_build_broker:
            with self.assertRaises(LiveTradingNotConfirmedError):
                build_live_session()

            # Refused before ever touching the broker factory.
            mock_build_broker.assert_not_called()

    @patch.dict(os.environ, {"LIVE_MARKET_DATA_PROVIDER": "groww"})
    def test_missing_credentials_rejected_only_when_groww_actually_selected(self) -> None:
        # broker stays paper (default), but market data explicitly asks
        # for groww -> credentials ARE required even though the broker
        # itself needs none.
        for var in ("GROWW_API_KEY", "GROWW_ACCESS_TOKEN"):
            os.environ.pop(var, None)

        with patch("apps.live_trader.main.build_broker") as mock_build_broker:
            with self.assertRaises(MissingGrowwCredentialsError):
                build_live_session()

            mock_build_broker.assert_not_called()

    @patch.dict(os.environ, GROWW_LIVE_BROKER_ENV)
    def test_broker_factory_is_invoked_with_groww_live_config(self) -> None:
        with patch(
            "apps.live_trader.main.build_broker", return_value=PaperBroker()
        ) as mock_build_broker:
            build_live_session()

        mock_build_broker.assert_called_once()
        (config,), _kwargs = mock_build_broker.call_args
        self.assertIsInstance(config, BrokerConfig)
        self.assertEqual(config.provider, "groww")
        self.assertEqual(config.mode, "live")
        self.assertEqual(config.metadata["api_key"], "test-api-key")
        self.assertEqual(config.metadata["access_token"], "test-access-token")

    @patch.dict(os.environ, GROWW_LIVE_BROKER_ENV)
    def test_build_live_session_succeeds_with_real_factory_and_real_adapter(self) -> None:
        # No patching of build_broker here: the real factory really does
        # build a real GrowwBrokerAdapter(mode="live"). Building the
        # session context alone never calls a broker method (no order is
        # placed until run_session() streams bars), so this succeeds even
        # though the transport is pointed at no real endpoint - it just
        # doesn't get exercised yet.
        ctx = build_live_session()

        self.assertIsInstance(ctx.broker, GrowwBrokerAdapter)
        self.assertEqual(ctx.broker.mode, "live")

    @patch.dict(os.environ, {**GROWW_LIVE_BROKER_ENV, "GROWW_BASE_URL": ""})
    def test_run_live_session_fails_fast_without_a_real_endpoint(self) -> None:
        # End-to-end, with nothing mocked: real factory -> real
        # GrowwBrokerAdapter + real GrowwLiveMarketDataFeed, both backed
        # by real (unmocked) HTTP transports. The market-data feed's
        # stream() is the very first thing the orchestrator touches, so
        # with no real base_url configured, *that* fails at URL-parsing
        # time - synchronously, zero network I/O - before any broker call
        # is even attempted (see test_groww_transport.py and
        # test_live_market_data_feed.py for the transport-level versions
        # of the same guarantee).
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(MarketDataTransportError):
                # NullAuditSink keeps this test hermetic (no real files
                # written to data/live_sessions/); persistence itself is
                # covered by AuditPersistenceLifecycleTests below and
                # test_audit_file_sink.py.
                run_live_session(audit_sink=NullAuditSink())

        self.assertIn("=== LIVE TRADER START ===", buffer.getvalue())

    @patch.dict(os.environ, GROWW_LIVE_BROKER_ENV)
    def test_session_uses_same_core_pipeline_as_paper_trader(self) -> None:
        with patch("apps.live_trader.main.build_broker", return_value=PaperBroker()):
            ctx = build_live_session()

        # Same building blocks the paper trader wires together: a real
        # allocation plan, a real selected universe, a real orchestrator,
        # and a session config describing the same strategy/symbols.
        self.assertIsNotNone(ctx.allocation_plan)
        self.assertEqual(ctx.universe.universe_name, "india_cash_v1")
        self.assertTrue(ctx.session_instruments)
        self.assertEqual(ctx.session_symbols, [i.symbol for i in ctx.session_instruments])

        self.assertIsInstance(ctx.orchestrator, ExecutionOrchestrator)
        self.assertEqual(ctx.session_config.strategy_name, "simple_breakout_momentum")
        self.assertEqual(ctx.session_config.symbols, ctx.session_symbols)
        self.assertEqual(ctx.session_config.trading_date, ctx.allocation_plan.trading_date)
        self.assertEqual(
            ctx.session_config.portfolio_allocation_plan.max_trades_per_day,
            ctx.allocation_plan.max_trades_per_day,
        )
        self.assertEqual(ctx.session_config.execution_mode, "live")
        self.assertEqual(ctx.broker_config.provider, "groww")
        self.assertEqual(ctx.broker_config.mode, "live")


class MarketDataWiringTests(unittest.TestCase):
    """
    Narrow integration-sanity tests: the live trader builds its market
    data feed via the market-data factory (never a concrete feed class
    directly) and registers it with MarketDataService, without changing
    orchestrator behavior. See core/market_data/factory.py and
    core/market_data/groww_live_feed.py for the underlying unit coverage.
    """

    @patch.dict(os.environ, VALID_ENV)
    def test_default_provider_is_memory_seeded_with_demo_bars(self) -> None:
        # LIVE_MARKET_DATA_PROVIDER not set -> safe local/dev default.
        ctx = build_live_session()

        self.assertEqual(ctx.market_data_config.provider, "memory")
        feed = ctx.orchestrator.market_data_service.active_feed
        self.assertIsInstance(feed, MemoryMarketDataFeed)
        self.assertIsNotNone(feed.get_latest_bar(ctx.session_symbols[0]))

    @patch.dict(os.environ, {**VALID_ENV, "LIVE_MARKET_DATA_PROVIDER": "groww"})
    def test_groww_provider_builds_live_feed_via_factory(self) -> None:
        ctx = build_live_session()

        self.assertEqual(ctx.market_data_config.provider, "groww")
        self.assertEqual(ctx.market_data_config.mode, "live")
        feed = ctx.orchestrator.market_data_service.active_feed
        self.assertIsInstance(feed, GrowwLiveMarketDataFeed)

    @patch.dict(os.environ, {**VALID_ENV, "LIVE_MARKET_DATA_PROVIDER": "GROWW"})
    def test_market_data_provider_env_var_is_case_insensitive(self) -> None:
        ctx = build_live_session()

        self.assertIsInstance(ctx.orchestrator.market_data_service.active_feed, GrowwLiveMarketDataFeed)


class AuditPersistenceLifecycleTests(unittest.TestCase):
    """
    Narrow tests proving run_live_session() owns the session persistence
    lifecycle correctly: started with best-effort metadata before the
    session context exists, enriched once it does, and failure recorded
    (with the exception still re-raised) regardless of where in the flow
    something goes wrong. File-based persistence mechanics are covered by
    test_audit_file_sink.py; this only checks the app's wiring.
    """

    @patch.dict(os.environ, {"LIVE_MARKET_DATA_PROVIDER": "groww", "GROWW_API_KEY": "", "GROWW_ACCESS_TOKEN": ""})
    def test_early_failure_before_context_still_starts_and_fails_the_session(self) -> None:
        sink = RecordingAuditSink()

        with self.assertRaises(MissingGrowwCredentialsError):
            run_live_session(audit_sink=sink)

        self.assertIsNotNone(sink.started_with)
        self.assertEqual(sink.started_with.app_name, "live_trader")
        # Context was never built, so the richer fields are still unset.
        self.assertIsNone(sink.started_with.broker_provider)

        self.assertIsInstance(sink.failed_with, MissingGrowwCredentialsError)
        self.assertIsNone(sink.finalized_with)

    @patch.dict(os.environ, {**GROWW_LIVE_BROKER_ENV, "GROWW_BASE_URL": ""})
    def test_mid_session_failure_records_rich_metadata_and_failure(self) -> None:
        sink = RecordingAuditSink()

        with redirect_stdout(io.StringIO()):
            with self.assertRaises(MarketDataTransportError):
                run_live_session(audit_sink=sink)

        # start_session() ran with the full context-derived snapshot.
        self.assertIsNotNone(sink.started_with)
        self.assertEqual(sink.started_with.broker_provider, "groww")
        self.assertEqual(sink.started_with.broker_mode, "live")
        self.assertTrue(sink.started_with.subscribed_symbols)

        # Events recorded before the crash (subscriptions at minimum) are present.
        self.assertTrue(sink.events)

        self.assertIsInstance(sink.failed_with, MarketDataTransportError)
        self.assertIsNone(sink.finalized_with)

    @patch.dict(os.environ, VALID_ENV)
    def test_successful_session_finalizes_with_a_summary_and_never_fails(self) -> None:
        sink = RecordingAuditSink()

        with redirect_stdout(io.StringIO()):
            with patch("apps.live_trader.main.build_broker", return_value=PaperBroker()):
                result = run_live_session(audit_sink=sink)

        self.assertIsNotNone(sink.started_with)
        self.assertIsNotNone(sink.finalized_with)
        self.assertTrue(sink.finalized_with.succeeded)
        self.assertEqual(sink.finalized_with.strategy_name, result.strategy_name)
        self.assertIsNone(sink.failed_with)


class StartupBannerTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=False)
    def test_banner_for_default_paper_and_memory_session(self) -> None:
        for var in ("GROWW_API_KEY", "GROWW_ACCESS_TOKEN", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        ctx = build_live_session()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_startup_banner(ctx)
        output = buffer.getvalue()

        self.assertIn("=== LIVE TRADER START ===", output)
        self.assertIn(f"Trading date: {ctx.allocation_plan.trading_date}", output)
        self.assertIn(f"Intraday capital: {ctx.allocation_plan.intraday_capital}", output)
        self.assertIn(f"Max daily loss: {ctx.allocation_plan.max_daily_loss}", output)
        self.assertIn(f"Max trades/day: {ctx.allocation_plan.max_trades_per_day}", output)
        self.assertIn(f"Max open positions: {ctx.allocation_plan.max_open_positions}", output)
        self.assertIn("Dry run: False", output)
        self.assertIn("Execution mode: paper", output)
        self.assertIn(f"Universe: {ctx.universe.universe_name}", output)
        self.assertIn(f"Subscribed symbols ({len(ctx.session_symbols)})", output)
        for symbol in ctx.session_symbols:
            self.assertIn(symbol, output)
        self.assertIn("Broker: paper/paper", output)
        self.assertIn("Market data: memory/paper", output)

    @patch.dict(os.environ, GROWW_LIVE_BROKER_ENV)
    def test_banner_for_groww_live_broker_session(self) -> None:
        with patch("apps.live_trader.main.build_broker", return_value=PaperBroker()):
            ctx = build_live_session()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_startup_banner(ctx)
        output = buffer.getvalue()

        self.assertIn("Broker: groww/live", output)
        self.assertIn("Execution mode: live", output)

    @patch.dict(os.environ, {"LIVE_DRY_RUN": "true"}, clear=False)
    def test_banner_reflects_dry_run_flag(self) -> None:
        for var in ("GROWW_API_KEY", "GROWW_ACCESS_TOKEN", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        ctx = build_live_session()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_startup_banner(ctx)

        self.assertIn("Dry run: True", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()

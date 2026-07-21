"""
Unit tests for apps.live_trader.

These are app-wiring tests: they confirm the live trader loads
credentials, invokes the broker factory with the right config, builds the
same core pipeline the paper trader uses, and prints a sane startup
banner - without ever performing real network I/O against the real
Groww API. Building a real GrowwBrokerAdapter or Groww market-data feed
now authenticates immediately (api_key+secret -> access token, see
core/broker/groww_transport.py), so any test that constructs one for
real mocks growwapi.GrowwAPI.get_access_token at the class level.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_live_trader.py -v
"""
from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from growwapi import GrowwAPI
from growwapi.groww.exceptions import GrowwAPIAuthenticationException, GrowwAPIBadRequestException

from apps.live_trader.config import (
    LIVE_CONFIRM_REQUIRED_VALUE,
    LiveTradingNotConfirmedError,
    MissingGrowwCredentialsError,
    load_groww_credentials_from_env,
)
from apps.live_trader.main import (
    LiveTransportNotReadyError,
    UncleanPriorSessionError,
    UnreconciledPositionsError,
    _ensure_live_transport_ready,
    build_live_session,
    print_startup_banner,
    run_live_session,
)
from core.audit.models import AuditEvent, SessionMetadata, SessionSummary
from core.audit.sink import AuditSink, NullAuditSink
from core.broker.base import OrderRequest, OrderSide, OrderType, ProductType
from core.broker.factory_models import BrokerConfig
from core.broker.groww_adapter import GrowwBrokerAdapter
from core.broker.groww_errors import GrowwAuthenticationError
from core.broker.groww_models import GrowwCredentials
from core.broker.paper import PaperBroker
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.errors import MarketDataProviderError
from core.market_data.groww_live_feed import GrowwLiveMarketDataFeed
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.notifications.alert_sink import NullAlertSink

VALID_ENV = {
    "GROWW_API_KEY": "test-api-key",
    # a valid base32 string - real Groww credentials are a TOTP seed
    # (see core/utils/totp.py), not an arbitrary string; get_access_token
    # itself is still mocked in every test here, so the computed code's
    # value doesn't matter, only that generate_totp() doesn't raise.
    "GROWW_API_SECRET": "ORSXG5BNMFYGSLLTMVRXEZLU",
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

# Canned single-candle response for the candidate price-history fetch
# build_live_session now always performs before bucket selection when
# market_data_provider="groww" (see apps/live_trader/main.py's
# _build_candidate_market_data_config) - same verified array shape used
# by test_groww_historical_feed.py.
SAMPLE_HISTORICAL_CANDLES_RESPONSE = {
    "candles": [[1767250800, 2495.0, 2505.0, 2490.0, 2500.0, 1000.0]]
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


class RecordingAlertSink(NullAlertSink):
    """In-memory AlertSink test double: no I/O, just records calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, message: str, *, severity: str = "error") -> None:
        self.sent.append((message, severity))


class LoadGrowwCredentialsFromEnvTests(unittest.TestCase):
    @patch.dict(os.environ, {"GROWW_API_KEY": "", "GROWW_API_SECRET": ""})
    def test_missing_both_credentials_raises(self) -> None:
        with self.assertRaises(MissingGrowwCredentialsError) as ctx:
            load_groww_credentials_from_env()

        self.assertIn("GROWW_API_KEY", str(ctx.exception))
        self.assertIn("GROWW_API_SECRET", str(ctx.exception))

    @patch.dict(os.environ, {"GROWW_API_KEY": "some-key", "GROWW_API_SECRET": ""})
    def test_missing_secret_raises(self) -> None:
        with self.assertRaises(MissingGrowwCredentialsError) as ctx:
            load_groww_credentials_from_env()

        self.assertIn("GROWW_API_SECRET", str(ctx.exception))
        self.assertNotIn("GROWW_API_KEY,", str(ctx.exception))

    @patch.dict(os.environ, VALID_ENV)
    def test_valid_credentials_are_loaded(self) -> None:
        credentials = load_groww_credentials_from_env()

        self.assertEqual(credentials.api_key, "test-api-key")
        self.assertEqual(credentials.secret, "ORSXG5BNMFYGSLLTMVRXEZLU")


class EnsureLiveTransportReadyTests(unittest.TestCase):
    """
    ``transport=object()`` is injected in every adapter built here: these
    tests only exercise the app-level readiness *gate* (mode +
    LIVE_TRANSPORT_IMPLEMENTED), never a transport method, so a dummy
    non-None transport safely skips the real (network-touching)
    authentication a real GrowwHttpTransport would perform at
    construction.
    """

    def test_real_groww_live_adapter_is_ready_now_that_transport_is_wired(self) -> None:
        adapter = GrowwBrokerAdapter(
            credentials=GrowwCredentials(api_key="k", secret="s"),
            mode="live",
            transport=object(),  # type: ignore[arg-type]
        )

        _ensure_live_transport_ready(adapter)  # must not raise

    def test_gate_still_blocks_when_transport_flag_is_false(self) -> None:
        # Proves the gate *mechanism* itself still works, independent of
        # Groww's current implemented/not-implemented state - e.g. if the
        # flag were ever reverted, or a future broker reuses this pattern.
        adapter = GrowwBrokerAdapter(
            credentials=GrowwCredentials(api_key="k", secret="s"),
            mode="live",
            transport=object(),  # type: ignore[arg-type]
        )

        with patch.object(GrowwBrokerAdapter, "LIVE_TRANSPORT_IMPLEMENTED", False):
            with self.assertRaises(LiveTransportNotReadyError):
                _ensure_live_transport_ready(adapter)

    def test_groww_paper_mode_is_not_gated(self) -> None:
        adapter = GrowwBrokerAdapter(
            credentials=GrowwCredentials(api_key="k", secret="s"),
            mode="paper",
            transport=object(),  # type: ignore[arg-type]
        )

        _ensure_live_transport_ready(adapter)  # must not raise

    def test_non_groww_broker_is_not_gated(self) -> None:
        _ensure_live_transport_ready(PaperBroker())  # must not raise


@patch.object(GrowwAPI, "get_access_token", return_value="fake-token")
class BuildLiveSessionTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=False)
    def test_paper_broker_and_memory_data_needs_no_credentials(self, mock_get_token) -> None:
        # The safe default: LIVE_BROKER_PROVIDER/LIVE_MARKET_DATA_PROVIDER
        # both unset -> paper + memory, which must build successfully with
        # zero Groww credentials in the environment at all.
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        ctx = build_live_session()

        self.assertIsInstance(ctx.broker, PaperBroker)
        self.assertEqual(ctx.broker_config.provider, "paper")
        self.assertEqual(ctx.market_data_config.provider, "memory")
        mock_get_token.assert_not_called()

    @patch.dict(
        os.environ,
        {**VALID_ENV, "LIVE_BROKER_PROVIDER": "groww", "LIVE_MARKET_DATA_PROVIDER": "groww"},
        clear=False,
    )
    def test_groww_live_broker_without_confirmation_is_rejected(self, mock_get_token) -> None:
        os.environ.pop("LIVE_CONFIRM", None)

        with patch("apps.live_trader.main.build_broker") as mock_build_broker:
            with self.assertRaises(LiveTradingNotConfirmedError):
                build_live_session()

            # Refused before ever touching the broker factory.
            mock_build_broker.assert_not_called()
        mock_get_token.assert_not_called()

    @patch.dict(os.environ, {"LIVE_MARKET_DATA_PROVIDER": "groww"})
    def test_missing_credentials_rejected_only_when_groww_actually_selected(self, mock_get_token) -> None:
        # broker stays paper (default), but market data explicitly asks
        # for groww -> credentials ARE required even though the broker
        # itself needs none.
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET"):
            os.environ.pop(var, None)

        with patch("apps.live_trader.main.build_broker") as mock_build_broker:
            with self.assertRaises(MissingGrowwCredentialsError):
                build_live_session()

            mock_build_broker.assert_not_called()

    @patch.dict(os.environ, GROWW_LIVE_BROKER_ENV)
    @patch.object(GrowwAPI, "get_historical_candles", return_value=SAMPLE_HISTORICAL_CANDLES_RESPONSE)
    def test_broker_factory_is_invoked_with_groww_live_config(
        self, mock_get_historical_candles, mock_get_token
    ) -> None:
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
        self.assertEqual(config.metadata["secret"], "ORSXG5BNMFYGSLLTMVRXEZLU")

    @patch.dict(os.environ, GROWW_LIVE_BROKER_ENV)
    @patch.object(GrowwAPI, "get_historical_candles", return_value=SAMPLE_HISTORICAL_CANDLES_RESPONSE)
    @patch.object(GrowwAPI, "get_positions_for_user", return_value={"positions": []})
    def test_build_live_session_succeeds_with_real_factory_and_real_adapter(
        self, mock_get_positions, mock_get_historical_candles, mock_get_token
    ) -> None:
        # No patching of build_broker here: the real factory really does
        # build a real GrowwBrokerAdapter(mode="live"). Authentication and
        # the reconciliation check's list_positions() call are mocked so
        # this stays network-free; everything downstream is real.
        ctx = build_live_session()

        self.assertIsInstance(ctx.broker, GrowwBrokerAdapter)
        self.assertEqual(ctx.broker.mode, "live")
        self.assertTrue(ctx.reconciliation.is_clean)

    @patch.dict(os.environ, GROWW_LIVE_BROKER_ENV)
    def test_run_live_session_fails_when_groww_rejects_credentials(self, mock_get_token) -> None:
        # End-to-end, with nothing mocked but the auth call itself: real
        # factory -> real GrowwBrokerAdapter -> real GrowwHttpTransport,
        # which authenticates at construction (before any order/quote
        # call). A real Groww rejection there (bad api_key/secret) fails
        # loudly and immediately, before the orchestrator ever streams a
        # bar - see test_groww_transport.py for the transport-level
        # version of the same guarantee.
        mock_get_token.side_effect = GrowwAPIAuthenticationException()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(GrowwAuthenticationError):
                # NullAuditSink/NullAlertSink keep this test hermetic (no
                # real files written to data/live_sessions/ or
                # data/alerts.log); persistence itself is covered by
                # AuditPersistenceLifecycleTests below and
                # test_audit_file_sink.py.
                run_live_session(audit_sink=NullAuditSink(), alert_sink=NullAlertSink())

        # Failed during build_live_session() itself (broker construction),
        # before the startup banner - which needs a full context - ever
        # prints.
        self.assertNotIn("=== LIVE TRADER START ===", buffer.getvalue())

    @patch.dict(os.environ, GROWW_LIVE_BROKER_ENV)
    @patch.object(GrowwAPI, "get_historical_candles", return_value=SAMPLE_HISTORICAL_CANDLES_RESPONSE)
    def test_session_uses_same_core_pipeline_as_paper_trader(
        self, mock_get_historical_candles, mock_get_token
    ) -> None:
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


def _broker_with_open_position(symbol: str = "RELIANCE") -> PaperBroker:
    """A PaperBroker that already holds one open position - simulates a broker
    whose real state has drifted from what a fresh session assumes."""
    broker = PaperBroker()
    broker.place_order(
        OrderRequest(
            symbol=symbol,
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=10,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            price=2500.0,
        )
    )
    return broker


class ReconciliationGateTests(unittest.TestCase):
    """
    Exercises the reconciliation gate through build_live_session() using a
    PaperBroker double pre-seeded with an open position - no real Groww
    call needed, since the gate only depends on Broker.list_positions().
    """

    @patch.dict(os.environ, {}, clear=False)
    def test_unreconciled_positions_block_session_start_by_default(self) -> None:
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        with patch("apps.live_trader.main.build_broker", return_value=_broker_with_open_position()):
            with self.assertRaises(UnreconciledPositionsError) as ctx:
                build_live_session()

        self.assertIn("RELIANCE", str(ctx.exception))

    @patch.dict(os.environ, {"LIVE_ALLOW_UNRECONCILED_POSITIONS": "true"}, clear=False)
    def test_allow_unreconciled_positions_override_proceeds(self) -> None:
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        with patch("apps.live_trader.main.build_broker", return_value=_broker_with_open_position()):
            ctx = build_live_session()

        self.assertFalse(ctx.reconciliation.is_clean)

    @patch.dict(os.environ, {}, clear=False)
    def test_clean_broker_reports_clean_reconciliation(self) -> None:
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        ctx = build_live_session()

        self.assertTrue(ctx.reconciliation.is_clean)

    @patch.dict(os.environ, {}, clear=False)
    def test_banner_reports_unreconciled_positions_when_allowed(self) -> None:
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)
        os.environ["LIVE_ALLOW_UNRECONCILED_POSITIONS"] = "true"

        with patch("apps.live_trader.main.build_broker", return_value=_broker_with_open_position()):
            ctx = build_live_session()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_startup_banner(ctx)

        self.assertIn("UNRECONCILED", buffer.getvalue())
        self.assertIn("RELIANCE", buffer.getvalue())

    @patch.dict(os.environ, {}, clear=False)
    def test_banner_reports_clean_reconciliation(self) -> None:
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        ctx = build_live_session()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_startup_banner(ctx)

        self.assertIn("Reconciliation: clean", buffer.getvalue())


class UncleanPriorSessionGateTests(unittest.TestCase):
    """
    Exercises the crash-detection gate through build_live_session(), with
    core.audit.file_sink.find_unclean_prior_session patched at the
    apps.live_trader.main import site so no test touches the real
    data/live_sessions/ directory (see AuditPersistenceLifecycleTests'
    equivalent hermeticity note).
    """

    @patch.dict(os.environ, {}, clear=False)
    @patch("apps.live_trader.main.find_unclean_prior_session", return_value="20260101T000000_dead")
    def test_unclean_prior_session_blocks_by_default(self, mock_find) -> None:
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        with self.assertRaises(UncleanPriorSessionError) as ctx:
            build_live_session()

        self.assertIn("20260101T000000_dead", str(ctx.exception))
        mock_find.assert_called_once()

    @patch.dict(os.environ, {"LIVE_ALLOW_UNCLEAN_PRIOR_SESSION": "true"}, clear=False)
    @patch("apps.live_trader.main.find_unclean_prior_session", return_value="20260101T000000_dead")
    def test_allow_unclean_prior_session_override_proceeds(self, mock_find) -> None:
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        ctx = build_live_session()  # must not raise

        self.assertTrue(ctx.live_config.allow_unclean_prior_session)

    @patch.dict(os.environ, {}, clear=False)
    @patch("apps.live_trader.main.find_unclean_prior_session", return_value=None)
    def test_no_unclean_prior_session_does_not_block(self, mock_find) -> None:
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        build_live_session()  # must not raise


@patch.object(GrowwAPI, "get_access_token", return_value="fake-token")
class RunLiveSessionAlertingTests(unittest.TestCase):
    """
    Confirms run_live_session() fires an alert on every path that ends a
    session without a real result - via RecordingAlertSink, no real I/O.
    """

    @patch.dict(os.environ, {"LIVE_MARKET_DATA_PROVIDER": "groww", "GROWW_API_KEY": "", "GROWW_API_SECRET": ""})
    def test_alerts_on_early_failure_before_context(self, mock_get_token) -> None:
        alerts = RecordingAlertSink()

        with self.assertRaises(MissingGrowwCredentialsError):
            run_live_session(audit_sink=NullAuditSink(), alert_sink=alerts)

        self.assertEqual(len(alerts.sent), 1)
        message, severity = alerts.sent[0]
        self.assertEqual(severity, "critical")
        self.assertIn("failed before it could start", message)

    @patch.dict(os.environ, GROWW_LIVE_BROKER_ENV)
    @patch.object(GrowwAPI, "get_historical_candles", return_value=SAMPLE_HISTORICAL_CANDLES_RESPONSE)
    @patch.object(GrowwAPI, "get_positions_for_user", return_value={"positions": []})
    def test_alerts_on_mid_session_failure(
        self, mock_get_positions, mock_get_historical_candles, mock_get_token
    ) -> None:
        alerts = RecordingAlertSink()

        with redirect_stdout(io.StringIO()):
            with patch.object(GrowwAPI, "get_quote", side_effect=GrowwAPIBadRequestException()):
                with self.assertRaises(MarketDataProviderError):
                    run_live_session(audit_sink=NullAuditSink(), alert_sink=alerts)

        self.assertEqual(len(alerts.sent), 1)
        message, severity = alerts.sent[0]
        self.assertEqual(severity, "critical")
        self.assertIn("died", message)

    @patch.dict(os.environ, VALID_ENV)
    def test_no_alert_on_a_clean_successful_session(self, mock_get_token) -> None:
        alerts = RecordingAlertSink()

        with redirect_stdout(io.StringIO()):
            with patch("apps.live_trader.main.build_broker", return_value=PaperBroker()):
                run_live_session(audit_sink=NullAuditSink(), alert_sink=alerts)

        self.assertEqual(alerts.sent, [])


@patch.object(GrowwAPI, "get_access_token", return_value="fake-token")
class MarketDataWiringTests(unittest.TestCase):
    """
    Narrow integration-sanity tests: the live trader builds its market
    data feed via the market-data factory (never a concrete feed class
    directly) and registers it with MarketDataService, without changing
    orchestrator behavior. See core/market_data/factory.py and
    core/market_data/groww_live_feed.py for the underlying unit coverage.
    """

    @patch.dict(os.environ, VALID_ENV)
    def test_default_provider_is_memory_seeded_with_demo_bars(self, mock_get_token) -> None:
        # LIVE_MARKET_DATA_PROVIDER not set -> safe local/dev default.
        ctx = build_live_session()

        self.assertEqual(ctx.market_data_config.provider, "memory")
        feed = ctx.orchestrator.market_data_service.active_feed
        self.assertIsInstance(feed, MemoryMarketDataFeed)
        self.assertIsNotNone(feed.get_latest_bar(ctx.session_symbols[0]))
        mock_get_token.assert_not_called()

    @patch.dict(os.environ, {**VALID_ENV, "LIVE_MARKET_DATA_PROVIDER": "groww"})
    @patch.object(GrowwAPI, "get_historical_candles", return_value=SAMPLE_HISTORICAL_CANDLES_RESPONSE)
    def test_groww_provider_builds_live_feed_via_factory(
        self, mock_get_historical_candles, mock_get_token
    ) -> None:
        ctx = build_live_session()

        self.assertEqual(ctx.market_data_config.provider, "groww")
        self.assertEqual(ctx.market_data_config.mode, "live")
        feed = ctx.orchestrator.market_data_service.active_feed
        self.assertIsInstance(feed, GrowwLiveMarketDataFeed)

    @patch.dict(os.environ, {**VALID_ENV, "LIVE_MARKET_DATA_PROVIDER": "GROWW"})
    @patch.object(GrowwAPI, "get_historical_candles", return_value=SAMPLE_HISTORICAL_CANDLES_RESPONSE)
    def test_market_data_provider_env_var_is_case_insensitive(
        self, mock_get_historical_candles, mock_get_token
    ) -> None:
        ctx = build_live_session()

        self.assertIsInstance(ctx.orchestrator.market_data_service.active_feed, GrowwLiveMarketDataFeed)


@patch.object(GrowwAPI, "get_access_token", return_value="fake-token")
class AuditPersistenceLifecycleTests(unittest.TestCase):
    """
    Narrow tests proving run_live_session() owns the session persistence
    lifecycle correctly: started with best-effort metadata before the
    session context exists, enriched once it does, and failure recorded
    (with the exception still re-raised) regardless of where in the flow
    something goes wrong. File-based persistence mechanics are covered by
    test_audit_file_sink.py; this only checks the app's wiring.
    """

    @patch.dict(os.environ, {"LIVE_MARKET_DATA_PROVIDER": "groww", "GROWW_API_KEY": "", "GROWW_API_SECRET": ""})
    def test_early_failure_before_context_still_starts_and_fails_the_session(self, mock_get_token) -> None:
        sink = RecordingAuditSink()

        with self.assertRaises(MissingGrowwCredentialsError):
            run_live_session(audit_sink=sink, alert_sink=NullAlertSink())

        self.assertIsNotNone(sink.started_with)
        self.assertEqual(sink.started_with.app_name, "live_trader")
        # Context was never built, so the richer fields are still unset.
        self.assertIsNone(sink.started_with.broker_provider)

        self.assertIsInstance(sink.failed_with, MissingGrowwCredentialsError)
        self.assertIsNone(sink.finalized_with)
        mock_get_token.assert_not_called()

    @patch.dict(os.environ, GROWW_LIVE_BROKER_ENV)
    @patch.object(GrowwAPI, "get_historical_candles", return_value=SAMPLE_HISTORICAL_CANDLES_RESPONSE)
    @patch.object(GrowwAPI, "get_positions_for_user", return_value={"positions": []})
    def test_mid_session_failure_records_rich_metadata_and_failure(
        self, mock_get_positions, mock_get_historical_candles, mock_get_token
    ) -> None:
        # Authentication succeeds (both broker and market-data transports
        # construct fine, so a full context - and its rich metadata - is
        # built) and reconciliation is clean, but the market-data feed's
        # first real quote call fails once the orchestrator starts
        # streaming bars - a genuine mid-session failure, distinct from
        # the early (pre-context) failure case above.
        sink = RecordingAuditSink()

        with redirect_stdout(io.StringIO()):
            with patch.object(GrowwAPI, "get_quote", side_effect=GrowwAPIBadRequestException()):
                with self.assertRaises(MarketDataProviderError):
                    run_live_session(audit_sink=sink, alert_sink=NullAlertSink())

        # start_session() ran with the full context-derived snapshot.
        self.assertIsNotNone(sink.started_with)
        self.assertEqual(sink.started_with.broker_provider, "groww")
        self.assertEqual(sink.started_with.broker_mode, "live")
        self.assertTrue(sink.started_with.subscribed_symbols)

        # Events recorded before the crash (subscriptions at minimum) are present.
        self.assertTrue(sink.events)

        self.assertIsInstance(sink.failed_with, MarketDataProviderError)
        self.assertIsNone(sink.finalized_with)

    @patch.dict(os.environ, VALID_ENV)
    def test_successful_session_finalizes_with_a_summary_and_never_fails(self, mock_get_token) -> None:
        sink = RecordingAuditSink()

        with redirect_stdout(io.StringIO()):
            with patch("apps.live_trader.main.build_broker", return_value=PaperBroker()):
                result = run_live_session(audit_sink=sink, alert_sink=NullAlertSink())

        self.assertIsNotNone(sink.started_with)
        self.assertIsNotNone(sink.finalized_with)
        self.assertTrue(sink.finalized_with.succeeded)
        self.assertEqual(sink.finalized_with.strategy_name, result.strategy_name)
        self.assertIsNone(sink.failed_with)
        mock_get_token.assert_not_called()  # LIVE_MARKET_DATA_PROVIDER unset -> memory, no groww touch


@patch.object(GrowwAPI, "get_access_token", return_value="fake-token")
class StartupBannerTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=False)
    def test_banner_for_default_paper_and_memory_session(self, mock_get_token) -> None:
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
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
    @patch.object(GrowwAPI, "get_historical_candles", return_value=SAMPLE_HISTORICAL_CANDLES_RESPONSE)
    def test_banner_for_groww_live_broker_session(
        self, mock_get_historical_candles, mock_get_token
    ) -> None:
        with patch("apps.live_trader.main.build_broker", return_value=PaperBroker()):
            ctx = build_live_session()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_startup_banner(ctx)
        output = buffer.getvalue()

        self.assertIn("Broker: groww/live", output)
        self.assertIn("Execution mode: live", output)

    @patch.dict(os.environ, {"LIVE_DRY_RUN": "true"}, clear=False)
    def test_banner_reflects_dry_run_flag(self, mock_get_token) -> None:
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        ctx = build_live_session()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_startup_banner(ctx)

        self.assertIn("Dry run: True", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()

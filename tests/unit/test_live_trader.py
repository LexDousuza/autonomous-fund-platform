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
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from growwapi import GrowwAPI
from growwapi.groww.exceptions import GrowwAPIAuthenticationException, GrowwAPIBadRequestException
from kiteconnect import KiteConnect

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
from core.broker.estimated_margin import EstimatedSpanMarginCalculator
from core.broker.factory_models import BrokerConfig
from core.broker.groww_adapter import GrowwBrokerAdapter
from core.broker.groww_errors import GrowwAuthenticationError
from core.broker.groww_models import GrowwCredentials
from core.broker.paper import PaperBroker
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.candidate_profile import CandidateProfile
from core.market_data.candidate_selection_store import CandidateSelection
from core.market_data.errors import MarketDataProviderError
from core.market_data.groww_live_feed import GrowwLiveMarketDataFeed
from core.market_data.kite_option_chain import KiteOptionChainProvider
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.universe_models import Instrument
from core.news.google_news_feed import NewsItem
from core.risk.options_margin import OptionsMarginRiskManager
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
    # SEBI's algo-tag gate (see apps/live_trader/config.py's
    # MissingBrokerAlgoIdError) - any non-blank value satisfies it here,
    # its exact content is never asserted on by these tests.
    "LIVE_BROKER_ALGO_ID": "TEST-ALGO-ID",
}

# Canned single-candle response for the candidate price-history fetch
# build_live_session now always performs before bucket selection when
# market_data_provider="groww" (see apps/live_trader/main.py's
# _build_candidate_market_data_config) - same verified array shape used
# by test_groww_historical_feed.py.
SAMPLE_HISTORICAL_CANDLES_RESPONSE = {
    "candles": [[1767250800, 2495.0, 2505.0, 2490.0, 2500.0, 1000.0]]
}

# build_live_session also runs a best-effort fo_eligible IV screen before
# bucket selection now (see core/market_data/fo_iv_screen.py) - patched
# module-wide (not per-test) so every existing groww-provider test here
# keeps its "no real network I/O" guarantee without touching each test's
# own mock decorators/signatures. An empty expiries list makes the
# screen a no-op past this one call.
_no_expiries_patcher = patch.object(GrowwAPI, "get_expiries", return_value={"expiries": []})

# build_live_session also checks for an overnight-precomputed candidate
# selection now (see core/market_data/candidate_selection_store.py) -
# patched module-wide, same reasoning as _no_expiries_patcher above: a
# real data/candidate_selection/<date>.json on this machine (e.g. from
# an actual overnight run) must never silently change what these tests
# exercise. BuildLiveSessionPersistedSelectionTests overrides this
# per-test (a closer decorator wins) to actually test the fast path.
_no_persisted_selection_patcher = patch("apps.live_trader.main.load_candidate_selection", return_value=None)


def setUpModule() -> None:
    _no_expiries_patcher.start()
    _no_persisted_selection_patcher.start()


def tearDownModule() -> None:
    _no_expiries_patcher.stop()
    _no_persisted_selection_patcher.stop()


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


# Env for a real Kite live broker path, market data routed through Kite
# too (an allowed combination - see _ALLOWED_PROVIDER_COMBINATIONS).
# build_market_data_feed is always patched alongside this so no real
# Kite market-data call is attempted.
KITE_LIVE_BROKER_ENV = {
    "KITE_API_KEY": "test-kite-key",
    "KITE_ACCESS_TOKEN": "test-kite-token",
    "LIVE_BROKER_PROVIDER": "kite",
    "LIVE_MARKET_DATA_PROVIDER": "kite",
    "LIVE_CONFIRM": LIVE_CONFIRM_REQUIRED_VALUE,
    "LIVE_BROKER_ALGO_ID": "TEST-ALGO-ID",
}


class _FakeKiteTransportForMargin:
    """Stands in for a real KiteHttpTransport: only get_margins() is
    exercised by the wiring under test."""

    def __init__(self, net_margin: float) -> None:
        self.net_margin = net_margin
        self.segments_requested: list[str | None] = []

    def get_margins(self, segment: str | None = None) -> dict:
        self.segments_requested.append(segment)
        return {"net": self.net_margin}


class _FakeKiteBrokerForMargin(PaperBroker):
    """A broker with a `.transport` attribute (what build_live_session's
    margin wiring reads) but PaperBroker's real, network-free behavior
    for everything else (list_positions, place_order)."""

    def __init__(self, transport: object) -> None:
        super().__init__()
        self.transport = transport


class AvailableMarginWiringTests(unittest.TestCase):
    """
    core.broker.kite_margin.fetch_available_margin threaded into
    TradingSessionConfig.available_margin for real Kite sessions only -
    see core/execution/orchestrator.py's OptionsMarginRiskManager
    consumer and this repo's practicality-audit history of naked writes
    being unconditionally rejected while this value was always None.
    """

    @patch.dict(os.environ, KITE_LIVE_BROKER_ENV)
    @patch.object(KiteConnect, "profile", return_value={"user_id": "TEST"})
    @patch("apps.live_trader.main.build_market_data_feed", return_value=MemoryMarketDataFeed(bars=[]))
    @patch(
        "apps.live_trader.candidate_selection.build_market_data_feed",
        return_value=MemoryMarketDataFeed(bars=[]),
    )
    def test_kite_session_threads_fetched_margin_into_session_config(
        self, mock_build_candidate_feed, mock_build_feed, mock_profile
    ) -> None:
        # KiteMarketDataTransport (built for the option-chain-provider
        # injection, a separate concern from the fake broker/transport
        # below) really does authenticate against kiteconnect.KiteConnect
        # at construction - mocked at the SDK level so this stays
        # network-free, same reasoning as GrowwAPI.get_access_token above.
        fake_transport = _FakeKiteTransportForMargin(net_margin=87654.0)
        with patch(
            "apps.live_trader.main.build_broker",
            return_value=_FakeKiteBrokerForMargin(fake_transport),
        ):
            ctx = build_live_session()

        self.assertEqual(ctx.session_config.available_margin, 87654.0)
        self.assertEqual(fake_transport.segments_requested, ["equity"])
        self.assertIsInstance(
            ctx.session_config.strategy_params["option_chain_provider"], KiteOptionChainProvider
        )

    @patch.dict(os.environ, GROWW_LIVE_BROKER_ENV)
    @patch.object(GrowwAPI, "get_access_token", return_value="fake-token")
    @patch.object(GrowwAPI, "get_historical_candles", return_value=SAMPLE_HISTORICAL_CANDLES_RESPONSE)
    def test_groww_session_leaves_available_margin_unset(
        self, mock_get_historical_candles, mock_get_token
    ) -> None:
        # Groww's account-level margin fetch is a separate, out-of-scope
        # gap (GrowwMarketDataTransport.get_available_margin_details()
        # exists but isn't wired) - confirms this Kite-only change didn't
        # accidentally start populating it for Groww too.
        with patch("apps.live_trader.main.build_broker", return_value=PaperBroker()):
            ctx = build_live_session()

        self.assertIsNone(ctx.session_config.available_margin)

    @patch.dict(os.environ, {}, clear=False)
    def test_paper_provider_gets_margin_aware_risk_manager_and_simulated_margin(self) -> None:
        # Default provider (paper broker + memory market data) - needs no
        # credentials at all, matching BuildLiveSessionTests's identical
        # default-config precedent.
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        ctx = build_live_session()

        risk_manager = ctx.orchestrator.risk_manager
        self.assertIsInstance(risk_manager, OptionsMarginRiskManager)
        self.assertIsInstance(risk_manager.margin_calculator, EstimatedSpanMarginCalculator)
        self.assertEqual(ctx.session_config.available_margin, ctx.allocation_plan.intraday_capital)


class FixedUniverseSegmentBypassTests(unittest.TestCase):
    """
    CandidateProfile.fixed_universe_segment must skip the entire cash-
    equity prescreen/historical-fetch/bucket-selection pipeline and load
    the fixed index universe instead - see that field's docstring.
    """

    @patch.dict(os.environ, {}, clear=False)
    @patch(
        "apps.live_trader.main.load_strategy_config",
        return_value=("index_options_test_strategy", {}, CandidateProfile(fixed_universe_segment="INDEX")),
    )
    def test_selects_index_instruments_and_skips_prescreen(self, mock_load_strategy_config) -> None:
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        ctx = build_live_session()

        self.assertEqual(set(ctx.session_symbols), {"NIFTY", "BANKNIFTY"})
        self.assertIsNone(ctx.prescreen_result)
        self.assertFalse(ctx.used_precomputed_selection)

    @patch.dict(os.environ, {}, clear=False)
    @patch(
        "apps.live_trader.main.load_strategy_config",
        return_value=("index_options_test_strategy", {}, CandidateProfile(fixed_universe_segment="INDEX")),
    )
    @patch("apps.live_trader.main.load_candidate_selection")
    def test_never_consults_persisted_or_live_candidate_selection(
        self, mock_load_candidate_selection, mock_load_strategy_config
    ) -> None:
        for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "LIVE_BROKER_PROVIDER", "LIVE_MARKET_DATA_PROVIDER"):
            os.environ.pop(var, None)

        build_live_session()

        mock_load_candidate_selection.assert_not_called()


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


_ANGELONE_ENV = {
    "ANGELONE_API_KEY": "test-api-key",
    "ANGELONE_CLIENT_CODE": "C123",
    "ANGELONE_PASSWORD": "1234",
    "ANGELONE_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
    "LIVE_BROKER_PROVIDER": "paper",
    "LIVE_MARKET_DATA_PROVIDER": "angelone",
}

_ANGELONE_SCRIP_MASTER_FIXTURE = [
    {"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "instrumenttype": "", "exch_seg": "NSE"},
    {"token": "1594", "symbol": "INFY-EQ", "name": "INFY", "instrumenttype": "", "exch_seg": "NSE"},
]


class FakeAngelOneClientForLiveSession:
    """
    Test double covering every SmartConnect call build_live_session's
    Angel One path can make: auth, the prescreen's bulk LTP, and the
    candidate historical fetch. Deliberately excludes ltpData - the live
    feed authenticates at construction but build_live_session never
    calls .stream(), so a single-symbol LTP poll never happens here.
    """

    def generateSession(self, client_code, password, totp):
        return {"status": True, "data": {"jwtToken": "jwt", "refreshToken": "refresh", "feedToken": "feed"}}

    def getMarketData(self, mode, exchangeTokens):
        # RELIANCE priced affordable, INFY priced above the test mandate's
        # max_capital_per_trade - exercises both prescreen outcomes in one call.
        return {
            "status": True,
            "message": "SUCCESS",
            "errorcode": "",
            "data": {
                "fetched": [
                    {"exchange": "NSE", "tradingSymbol": "RELIANCE-EQ", "symbolToken": "2885", "ltp": 100.0},
                    {"exchange": "NSE", "tradingSymbol": "INFY-EQ", "symbolToken": "1594", "ltp": 999999.0},
                ],
                "unfetched": [],
            },
        }

    def getCandleData(self, params):
        return {
            "status": True,
            "message": "SUCCESS",
            "errorcode": "",
            "data": [["2026-01-01T09:15:00+05:30", 100.0, 101.0, 99.0, 100.5, 1000]],
        }


@contextmanager
def _patched_angelone_scrip_master():
    body = json.dumps(_ANGELONE_SCRIP_MASTER_FIXTURE).encode("utf-8")

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return body

    with patch("urllib.request.urlopen", return_value=_FakeResponse()):
        yield


def _make_two_symbol_universe_path(test_case: unittest.TestCase) -> str:
    """Shared by every test that needs a tiny real universe file instead of the ~2400-symbol production one."""
    tmp_dir = tempfile.TemporaryDirectory()
    test_case.addCleanup(tmp_dir.cleanup)
    path = Path(tmp_dir.name) / "universe.yaml"
    path.write_text(
        "universe_name: two_symbol_test\n"
        "instruments:\n"
        "  - symbol: RELIANCE\n"
        "    exchange: NSE\n"
        "    segment: CASH\n"
        "    enabled: true\n"
        "  - symbol: INFY\n"
        "    exchange: NSE\n"
        "    segment: CASH\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    return str(path)


class BuildLiveSessionAngelOnePrescreenTests(unittest.TestCase):
    """
    apps.live_trader.main's Angel One-only candidate-prescreen wiring
    (see core/market_data/candidate_prescreen.py) - a real end-to-end
    build_live_session() call against a tiny 2-symbol test universe, not
    the full ~2400-symbol production one (impractical to fake at that
    scale), with a bullish canned news item for RELIANCE so it's
    deterministic which symbol survives the news cut too.
    """

    def _universe_path(self) -> str:
        return _make_two_symbol_universe_path(self)

    @patch.dict(os.environ, _ANGELONE_ENV, clear=False)
    @patch("core.market_data.candidate_prescreen.GoogleNewsFeed")
    @patch("core.market_data.angelone_transport.SmartConnect", return_value=FakeAngelOneClientForLiveSession())
    def test_prescreen_narrows_candidates_before_bucket_selection(
        self, mock_smart_connect, mock_news_feed_cls
    ) -> None:
        mock_news_feed_cls.return_value.fetch.return_value = [
            NewsItem(
                query="RELIANCE",
                headline="Stock surges on record profit growth",
                link="",
                source=None,
                published_at=None,
            )
        ]

        with _patched_angelone_scrip_master():
            ctx = build_live_session(universe_path=self._universe_path())

        self.assertEqual(ctx.market_data_config.provider, "angelone")
        self.assertIsNotNone(ctx.prescreen_result)
        # INFY excluded as unaffordable before any news call was even made for it;
        # RELIANCE survives (affordable + bullish news) into bucket selection.
        self.assertEqual([i.symbol for i in ctx.prescreen_result.excluded_unaffordable], ["INFY"])
        self.assertEqual([i.symbol for i in ctx.candidate_instruments], ["RELIANCE"])
        mock_news_feed_cls.return_value.fetch.assert_called_once()

    @patch.dict(os.environ, _ANGELONE_ENV, clear=False)
    @patch("core.market_data.candidate_prescreen.GoogleNewsFeed")
    @patch("core.market_data.angelone_transport.SmartConnect", return_value=FakeAngelOneClientForLiveSession())
    def test_banner_shows_prescreen_section_for_angelone(self, mock_smart_connect, mock_news_feed_cls) -> None:
        mock_news_feed_cls.return_value.fetch.return_value = []

        with _patched_angelone_scrip_master():
            ctx = build_live_session(universe_path=self._universe_path())

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_startup_banner(ctx)
        output = buffer.getvalue()

        self.assertIn("=== CANDIDATE PRESCREEN ===", output)
        self.assertIn("excluded unaffordable: 1", output)

    @patch.dict(os.environ, {**_ANGELONE_ENV, "LIVE_ANGELONE_LIVE_MODE": "candles"}, clear=False)
    @patch("core.market_data.candidate_prescreen.GoogleNewsFeed")
    @patch("core.market_data.angelone_transport.SmartConnect", return_value=FakeAngelOneClientForLiveSession())
    def test_angelone_live_mode_candles_builds_live_candles_market_data_config(
        self, mock_smart_connect, mock_news_feed_cls
    ) -> None:
        mock_news_feed_cls.return_value.fetch.return_value = []

        with _patched_angelone_scrip_master():
            ctx = build_live_session(universe_path=self._universe_path())

        self.assertEqual(ctx.market_data_config.provider, "angelone")
        self.assertEqual(ctx.market_data_config.mode, "live_candles")


class FakeAngelOneClientRejectingSelectionCalls:
    """
    Only implements generateSession (needed to construct the live feed,
    which authenticates regardless of where session_instruments came
    from) - getMarketData/getCandleData raise if called at all, proving
    a persisted selection genuinely skips prescreen and the historical
    fetch rather than merely ignoring their result.
    """

    def generateSession(self, client_code, password, totp):
        return {"status": True, "data": {"jwtToken": "jwt", "refreshToken": "refresh", "feedToken": "feed"}}

    def getMarketData(self, mode, exchangeTokens):
        raise AssertionError("getMarketData (prescreen) must not be called when a selection was already persisted")

    def getCandleData(self, params):
        raise AssertionError("getCandleData (historical fetch) must not be called when a selection was already persisted")


class BuildLiveSessionPersistedSelectionTests(unittest.TestCase):
    """
    apps.live_trader.main's fast path for an overnight-precomputed
    selection (see apps/live_trader/select_candidates_main.py and
    core/market_data/candidate_selection_store.py) - the market-open
    session should use it directly and skip prescreen/historical/bucket
    selection entirely, not just prefer it.
    """

    @patch.dict(os.environ, _ANGELONE_ENV, clear=False)
    @patch(
        "apps.live_trader.main.load_candidate_selection",
        return_value=CandidateSelection(
            trading_date="2026-01-01",
            computed_at="2026-01-01T00:00:00+00:00",
            instruments=[Instrument(symbol="RELIANCE", exchange="NSE")],
        ),
    )
    @patch("core.market_data.angelone_transport.SmartConnect", return_value=FakeAngelOneClientRejectingSelectionCalls())
    def test_persisted_selection_is_used_and_skips_prescreen_and_historical_fetch(
        self, mock_smart_connect, mock_load_selection
    ) -> None:
        ctx = build_live_session()

        self.assertTrue(ctx.used_precomputed_selection)
        self.assertIsNone(ctx.prescreen_result)
        self.assertEqual([i.symbol for i in ctx.session_instruments], ["RELIANCE"])
        self.assertEqual(ctx.session_symbols, ["RELIANCE"])
        mock_load_selection.assert_called_once()

    @patch.dict(os.environ, _ANGELONE_ENV, clear=False)
    @patch(
        "apps.live_trader.main.load_candidate_selection",
        return_value=CandidateSelection(trading_date="2026-01-01", computed_at="2026-01-01T00:00:00+00:00", instruments=[]),
    )
    @patch("core.market_data.angelone_transport.SmartConnect", return_value=FakeAngelOneClientRejectingSelectionCalls())
    def test_persisted_empty_selection_produces_empty_session_not_a_crash(
        self, mock_smart_connect, mock_load_selection
    ) -> None:
        ctx = build_live_session()

        self.assertTrue(ctx.used_precomputed_selection)
        self.assertEqual(ctx.session_instruments, [])
        self.assertEqual(ctx.session_symbols, [])

    @patch.dict(os.environ, _ANGELONE_ENV, clear=False)
    @patch("apps.live_trader.main.load_candidate_selection", return_value=None)
    @patch("core.market_data.candidate_prescreen.GoogleNewsFeed")
    @patch("core.market_data.angelone_transport.SmartConnect", return_value=FakeAngelOneClientForLiveSession())
    def test_no_persisted_selection_falls_back_to_live_selection(
        self, mock_smart_connect, mock_news_feed_cls, mock_load_selection
    ) -> None:
        mock_news_feed_cls.return_value.fetch.return_value = [
            NewsItem(query="RELIANCE", headline="Stock surges on record profit growth", link="", source=None, published_at=None)
        ]

        with _patched_angelone_scrip_master():
            ctx = build_live_session(universe_path=_make_two_symbol_universe_path(self))

        self.assertFalse(ctx.used_precomputed_selection)
        self.assertIsNotNone(ctx.prescreen_result)


if __name__ == "__main__":
    unittest.main()

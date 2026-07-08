"""
Unit tests for ExecutionOrchestrator's audit-event emission
(core.execution.orchestrator + core.audit).

These tests use a small in-memory AuditSink test double - no file I/O -
to keep them fast and focused purely on "does the orchestrator emit the
right event, with the right payload, at the right moment." Persistence
behavior itself is covered by tests/unit/test_audit_file_sink.py.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_execution_audit.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from core.audit.models import AuditEvent, AuditEventType, SessionMetadata, SessionSummary
from core.audit.sink import AuditSink
from core.broker.base import OrderRequest
from core.broker.paper import PaperBroker
from core.execution.models import TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.portfolio.exit_models import ExitPolicyConfig
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.base import RiskContext, RiskDecision, RiskManager
from core.strategy.base import MarketBar, SignalSide, Strategy, StrategyContext, TradeSignal
from core.strategy.registry import StrategyRegistry


class InMemoryAuditSink(AuditSink):
    """Test double: records everything in memory, no file I/O."""

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

    def events_of_type(self, event_type: AuditEventType) -> list[AuditEvent]:
        return [e for e in self.events if e.event_type == event_type]


class AlwaysBuyStrategy(Strategy):
    """Test strategy that always emits a BUY signal, regardless of state."""

    def on_bar(self, bar, history, open_positions):
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_always_buy",
                confidence=0.7,
            )
        ]


class BuyOnceEverStrategy(Strategy):
    """Test strategy that emits exactly one BUY signal, on the first bar it sees."""

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._bought = False

    def on_bar(self, bar, history, open_positions):
        if self._bought:
            return []
        self._bought = True
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_buy_once",
            )
        ]


class FakeRiskManager(RiskManager):
    """Test risk manager with a fixed approve/reject outcome."""

    def __init__(self, approve: bool) -> None:
        self._approve = approve

    def evaluate(self, order_request: OrderRequest, context: RiskContext) -> RiskDecision:
        return RiskDecision(
            approved=self._approve,
            reason="test_approved" if self._approve else "test_risk_rejected",
        )


def make_bars(count: int, symbol: str = "TEST") -> list[MarketBar]:
    base_time = datetime(2026, 1, 1, 9, 15, 0)
    return [
        MarketBar(
            symbol=symbol,
            exchange="NSE",
            timestamp=base_time.replace(minute=15 + i),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0 + i,
            volume=1000.0,
        )
        for i in range(count)
    ]


def make_price_bars(closes: list[float], symbol: str = "TEST") -> list[MarketBar]:
    base_time = datetime(2026, 1, 1, 9, 15, 0)
    return [
        MarketBar(
            symbol=symbol,
            exchange="NSE",
            timestamp=base_time.replace(minute=15 + i),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1000.0,
        )
        for i, close in enumerate(closes)
    ]


def make_allocation_plan(
    max_capital_per_trade: float = 100.0,
    max_trades_per_day: int = 6,
    max_daily_loss: float = 2500.0,
    max_open_positions: int = 4,
    max_symbol_allocation: float | None = None,
) -> PortfolioAllocationPlan:
    return PortfolioAllocationPlan(
        trading_date=date(2026, 1, 1),
        total_capital=100000.0,
        reserve_capital=40000.0,
        intraday_capital=60000.0,
        max_daily_loss=max_daily_loss,
        max_trades_per_day=max_trades_per_day,
        max_capital_per_trade=max_capital_per_trade,
        max_open_positions=max_open_positions,
        max_symbol_allocation=(
            max_symbol_allocation if max_symbol_allocation is not None else max_capital_per_trade
        ),
        execution_mode="paper",
    )


def make_registry(strategy_name: str, strategy_cls) -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.register(strategy_name, strategy_cls)
    return registry


def make_orchestrator(
    bars: list[MarketBar],
    risk_manager: RiskManager,
    strategy_name: str,
    strategy_cls,
    audit_sink: AuditSink | None,
) -> tuple[ExecutionOrchestrator, PaperBroker]:
    market_data_service = MarketDataService()
    market_data_service.register_feed("test_feed", MemoryMarketDataFeed(bars=bars))

    broker = PaperBroker()
    orchestrator = ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=risk_manager,
        broker=broker,
        strategy_registry=make_registry(strategy_name, strategy_cls),
        audit_sink=audit_sink,
    )
    return orchestrator, broker


def make_config(
    strategy_name: str,
    symbols: list[str],
    allocation_plan: PortfolioAllocationPlan | None = None,
    exit_policy: ExitPolicyConfig | None = None,
) -> TradingSessionConfig:
    return TradingSessionConfig(
        trading_date=date(2026, 1, 1),
        strategy_name=strategy_name,
        strategy_params={},
        execution_mode="paper",
        symbols=symbols,
        portfolio_allocation_plan=allocation_plan or make_allocation_plan(),
        exit_policy=exit_policy,
    )


class NoSinkProvidedTests(unittest.TestCase):
    def test_orchestrator_runs_normally_without_a_sink(self) -> None:
        bars = make_bars(1)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy, audit_sink=None
        )
        config = make_config("always_buy", ["TEST"], allocation_plan=make_allocation_plan())

        result = orchestrator.run_session(config)  # must not raise

        self.assertEqual(result.approved_signals, 1)


class SignalEventTests(unittest.TestCase):
    def test_signal_generated_event_is_emitted(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_bars(1)
        orchestrator, _ = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy, audit_sink=sink
        )
        config = make_config("always_buy", ["TEST"])

        orchestrator.run_session(config)

        generated = sink.events_of_type(AuditEventType.SIGNAL_GENERATED)
        self.assertEqual(len(generated), 1)
        self.assertEqual(generated[0].symbol, "TEST")
        self.assertEqual(generated[0].payload["side"], "BUY")
        self.assertEqual(generated[0].payload["reason"], "test_always_buy")

    def test_signal_rejected_event_for_session_guardrail_block(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_price_bars([100.0] * 3)
        orchestrator, _ = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy, audit_sink=sink
        )
        config = make_config(
            "always_buy", ["TEST"], allocation_plan=make_allocation_plan(max_trades_per_day=1)
        )

        orchestrator.run_session(config)

        rejected = sink.events_of_type(AuditEventType.SIGNAL_REJECTED)
        guardrail_rejections = [e for e in rejected if e.payload["rejection_stage"] == "session_guardrail"]
        self.assertGreaterEqual(len(guardrail_rejections), 1)
        self.assertEqual(guardrail_rejections[0].payload["reason"], "max_trades_per_day_reached")

    def test_signal_rejected_event_for_sizing_zero_quantity(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_price_bars([500.0])  # price too high for a 100.0 capital budget
        orchestrator, _ = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy, audit_sink=sink
        )
        config = make_config("always_buy", ["TEST"], allocation_plan=make_allocation_plan(max_capital_per_trade=100.0))

        orchestrator.run_session(config)

        rejected = sink.events_of_type(AuditEventType.SIGNAL_REJECTED)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].payload["rejection_stage"], "sizing")
        self.assertEqual(rejected[0].payload["reason"], "price_exceeds_capital_per_trade")

    def test_signal_rejected_event_for_exposure_block(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_price_bars([100.0, 100.0])
        orchestrator, _ = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy, audit_sink=sink
        )
        # max_symbol_allocation=150 -> first buy (100) ok, second buy pushes to 200 > 150 -> blocked
        config = make_config(
            "always_buy",
            ["TEST"],
            allocation_plan=make_allocation_plan(max_capital_per_trade=100.0, max_symbol_allocation=150.0),
        )

        orchestrator.run_session(config)

        rejected = sink.events_of_type(AuditEventType.SIGNAL_REJECTED)
        exposure_rejections = [e for e in rejected if e.payload["rejection_stage"] == "exposure"]
        self.assertEqual(len(exposure_rejections), 1)
        self.assertEqual(exposure_rejections[0].payload["reason"], "max_symbol_allocation_exceeded")

    def test_signal_rejected_event_for_risk_manager_rejection(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_bars(1)
        orchestrator, _ = make_orchestrator(
            bars, FakeRiskManager(approve=False), "always_buy", AlwaysBuyStrategy, audit_sink=sink
        )
        config = make_config("always_buy", ["TEST"])

        orchestrator.run_session(config)

        rejected = sink.events_of_type(AuditEventType.SIGNAL_REJECTED)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].payload["rejection_stage"], "risk_manager")
        self.assertEqual(rejected[0].payload["reason"], "test_risk_rejected")

    def test_signal_approved_event_is_emitted(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_bars(1)
        orchestrator, _ = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy, audit_sink=sink
        )
        config = make_config("always_buy", ["TEST"])

        orchestrator.run_session(config)

        approved = sink.events_of_type(AuditEventType.SIGNAL_APPROVED)
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0].symbol, "TEST")
        self.assertEqual(approved[0].payload["quantity"], 1)


class OrderEventTests(unittest.TestCase):
    def test_order_submitted_and_placed_events_are_emitted_in_order(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_bars(1)
        orchestrator, _ = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy, audit_sink=sink
        )
        config = make_config("always_buy", ["TEST"])

        orchestrator.run_session(config)

        submitted = sink.events_of_type(AuditEventType.ORDER_SUBMITTED)
        placed = sink.events_of_type(AuditEventType.ORDER_PLACED)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0].payload["status"], "FILLED")
        self.assertEqual(placed[0].payload["quantity"], 1)
        self.assertIsNotNone(placed[0].payload["order_id"])

        # submitted must precede placed
        self.assertLess(sink.events.index(submitted[0]), sink.events.index(placed[0]))


class ExitEventTests(unittest.TestCase):
    def test_exit_triggered_and_exit_order_placed_events_are_emitted(self) -> None:
        sink = InMemoryAuditSink()
        # Bar 0: entry at 100. Bar 1: price drops 10% -> stop loss (5%) fires.
        bars = make_price_bars([100.0, 90.0])
        orchestrator, _ = make_orchestrator(
            bars, FakeRiskManager(approve=True), "buy_once", BuyOnceEverStrategy, audit_sink=sink
        )
        config = make_config(
            "buy_once",
            ["TEST"],
            allocation_plan=make_allocation_plan(max_capital_per_trade=100.0),
            exit_policy=ExitPolicyConfig(stop_loss_pct=5.0),
        )

        orchestrator.run_session(config)

        triggered = sink.events_of_type(AuditEventType.EXIT_TRIGGERED)
        exit_placed = sink.events_of_type(AuditEventType.EXIT_ORDER_PLACED)
        self.assertEqual(len(triggered), 1)
        self.assertEqual(triggered[0].payload["reason"], "stop_loss")
        self.assertEqual(len(exit_placed), 1)
        self.assertEqual(exit_placed[0].payload["status"], "FILLED")


class SessionGuardrailEventTests(unittest.TestCase):
    def test_session_entry_blocked_event_emitted_once_when_latched(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_price_bars([100.0] * 5)
        orchestrator, _ = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy, audit_sink=sink
        )
        config = make_config(
            "always_buy",
            ["TEST"],
            # max_symbol_allocation raised well above what repeated
            # same-symbol buys would need, so the exposure guardrail
            # never trips - this test isolates the session guardrail.
            allocation_plan=make_allocation_plan(max_trades_per_day=2, max_symbol_allocation=10000.0),
        )

        orchestrator.run_session(config)

        blocked_events = sink.events_of_type(AuditEventType.SESSION_ENTRY_BLOCKED)
        self.assertEqual(len(blocked_events), 1)  # emitted exactly once, not once per subsequent rejection
        self.assertEqual(blocked_events[0].payload["block_reason"], "max_trades_per_day_reached")
        self.assertEqual(blocked_events[0].payload["entries_taken"], 2)

    def test_symbols_subscribed_event_is_emitted(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_bars(1)
        orchestrator, _ = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy, audit_sink=sink
        )
        config = make_config("always_buy", ["TEST"])

        orchestrator.run_session(config)

        subscribed = sink.events_of_type(AuditEventType.SYMBOLS_SUBSCRIBED)
        self.assertEqual(len(subscribed), 1)
        self.assertEqual(subscribed[0].payload["symbols"], ["TEST"])


if __name__ == "__main__":
    unittest.main()

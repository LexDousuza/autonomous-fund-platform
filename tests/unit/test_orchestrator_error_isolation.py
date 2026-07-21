"""
Unit tests for ExecutionOrchestrator's per-bar error isolation: an
unexpected exception while processing one bar must not end the whole
multi-bar session (core.execution.orchestrator.run_session).

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_orchestrator_error_isolation.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from core.audit.models import AuditEvent, AuditEventType, SessionMetadata, SessionSummary
from core.audit.sink import AuditSink
from core.broker.paper import PaperBroker
from core.execution.models import TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.base import RiskContext, RiskDecision, RiskManager
from core.strategy.base import MarketBar, SignalSide, Strategy, StrategyContext, TradeSignal
from core.strategy.registry import StrategyRegistry


class InMemoryAuditSink(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def start_session(self, metadata: SessionMetadata) -> None:
        pass

    def record_event(self, event: AuditEvent) -> None:
        self.events.append(event)

    def finalize_session(self, summary: SessionSummary) -> None:
        pass

    def record_failure(self, error: BaseException) -> None:
        pass

    def events_of_type(self, event_type: AuditEventType) -> list[AuditEvent]:
        return [e for e in self.events if e.event_type == event_type]


class FailOnSecondBarStrategy(Strategy):
    """Simulates a strategy/data bug that blows up processing exactly one bar."""

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._bar_count = 0

    def on_bar(self, bar, history, open_positions):
        self._bar_count += 1
        if self._bar_count == 2:
            raise RuntimeError("simulated transient failure")
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_buy",
            )
        ]


class AlwaysFailsStrategy(Strategy):
    def on_bar(self, bar, history, open_positions):
        raise RuntimeError("always broken")


class RecordHistoryLengthStrategy(Strategy):
    """
    Records the history length it sees on each call (class-level, since
    the orchestrator - not the test - constructs the strategy instance
    via the registry); raises on bar 2.
    """

    history_lengths_seen: list[int] = []

    def on_bar(self, bar, history, open_positions):
        RecordHistoryLengthStrategy.history_lengths_seen.append(len(history))
        if len(RecordHistoryLengthStrategy.history_lengths_seen) == 2:
            raise RuntimeError("simulated transient failure")
        return []


class FakeRiskManager(RiskManager):
    def evaluate(self, order_request, context: RiskContext) -> RiskDecision:
        return RiskDecision(approved=True, reason="test_approved")


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


def make_allocation_plan() -> PortfolioAllocationPlan:
    return PortfolioAllocationPlan(
        trading_date=date(2026, 1, 1),
        total_capital=100000.0,
        reserve_capital=40000.0,
        intraday_capital=60000.0,
        max_daily_loss=2500.0,
        max_trades_per_day=6,
        max_capital_per_trade=1000.0,
        max_open_positions=4,
        max_symbol_allocation=1000.0,
        execution_mode="paper",
    )


def make_orchestrator(bars, strategy_cls, audit_sink=None) -> ExecutionOrchestrator:
    market_data_service = MarketDataService()
    market_data_service.register_feed("test_feed", MemoryMarketDataFeed(bars=bars))
    registry = StrategyRegistry()
    registry.register("test_strategy", strategy_cls)
    return ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=FakeRiskManager(),
        broker=PaperBroker(),
        strategy_registry=registry,
        audit_sink=audit_sink,
    )


def make_config(symbols: list[str]) -> TradingSessionConfig:
    return TradingSessionConfig(
        trading_date=date(2026, 1, 1),
        strategy_name="test_strategy",
        strategy_params={},
        execution_mode="paper",
        symbols=symbols,
        portfolio_allocation_plan=make_allocation_plan(),
    )


class PerBarErrorIsolationTests(unittest.TestCase):
    def test_one_bad_bar_does_not_end_the_session(self) -> None:
        bars = make_bars(4)
        orchestrator = make_orchestrator(bars, FailOnSecondBarStrategy)

        result = orchestrator.run_session(make_config(["TEST"]))  # must not raise

        self.assertEqual(result.processed_bars, 4)
        self.assertEqual(result.errors_encountered, 1)
        # bars 1, 3, 4 each successfully asked the strategy for a signal
        # (bar 2's exception happened before returning one) - proving the
        # session kept running after the error, not just that it didn't crash
        self.assertEqual(result.generated_signals, 3)

    def test_errored_bar_still_appended_to_symbol_history(self) -> None:
        RecordHistoryLengthStrategy.history_lengths_seen = []
        bars = make_bars(3)
        orchestrator = make_orchestrator(bars, RecordHistoryLengthStrategy)

        orchestrator.run_session(make_config(["TEST"]))  # must not raise

        # bar 1: history=[] (len 0); bar 2: history=[bar1] (len 1), raises;
        # bar 3: history=[bar1, bar2] (len 2) - bar 2 must have been
        # appended to history despite raising, or bar 3 would see len 1.
        self.assertEqual(RecordHistoryLengthStrategy.history_lengths_seen, [0, 1, 2])

    def test_orchestrator_error_event_emitted_with_symbol_and_error_type(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_bars(2)
        orchestrator = make_orchestrator(bars, FailOnSecondBarStrategy, audit_sink=sink)

        orchestrator.run_session(make_config(["TEST"]))

        error_events = sink.events_of_type(AuditEventType.ORCHESTRATOR_ERROR)
        self.assertEqual(len(error_events), 1)
        self.assertEqual(error_events[0].symbol, "TEST")
        self.assertEqual(error_events[0].payload["error_type"], "RuntimeError")
        self.assertIn("simulated transient failure", error_events[0].payload["message"])

    def test_every_bar_failing_still_completes_the_session(self) -> None:
        bars = make_bars(3)
        orchestrator = make_orchestrator(bars, AlwaysFailsStrategy)

        result = orchestrator.run_session(make_config(["TEST"]))  # must not raise

        self.assertEqual(result.processed_bars, 3)
        self.assertEqual(result.errors_encountered, 3)
        self.assertEqual(result.approved_signals, 0)


if __name__ == "__main__":
    unittest.main()

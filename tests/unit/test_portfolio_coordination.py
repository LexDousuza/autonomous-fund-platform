"""
Unit tests for ExecutionOrchestrator.run_portfolio_session: multi-strategy
symbol exclusivity, per-strategy capital/loss-limit isolation, and the
shared broker-error kill switch.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_portfolio_coordination.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from core.audit.models import AuditEvent, AuditEventType, SessionMetadata, SessionSummary
from core.audit.sink import AuditSink
from core.broker.paper import PaperBroker
from core.execution.models import TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.portfolio.exit_models import ExitPolicyConfig
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.base import RiskDecision, RiskManager
from core.strategy.base import MarketBar, SignalSide, Strategy, TradeSignal
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


class AlwaysBuyStrategy(Strategy):
    def on_bar(self, bar, history, open_positions):
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_always_buy",
            )
        ]


class ReBuyWhenFlatStrategy(Strategy):
    """Buys whenever it does not currently hold a position in this bar's
    symbol - used to prove a re-entry attempt gets guardrail-blocked."""

    def on_bar(self, bar, history, open_positions):
        key = f"{bar.exchange}:{bar.symbol}"
        if key in open_positions:
            return []
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_rebuy_when_flat",
            )
        ]


class AlwaysFailsStrategy(Strategy):
    """Simulates a persistent transport/strategy fault - every bar raises."""

    def on_bar(self, bar, history, open_positions):
        raise RuntimeError("simulated persistent failure")


class FakeRiskManager(RiskManager):
    def __init__(self, approve: bool = True) -> None:
        self._approve = approve

    def evaluate(self, order_request, context):
        return RiskDecision(approved=self._approve, reason="test")


def make_registry(*entries: tuple[str, type]) -> StrategyRegistry:
    registry = StrategyRegistry()
    for name, cls in entries:
        registry.register(name, cls)
    return registry


def make_plan(
    max_capital_per_trade: float = 1000.0,
    max_daily_loss: float = 100000.0,
    max_trades_per_day: int = 50,
    max_open_positions: int = 10,
    max_symbol_allocation: float | None = None,
    max_consecutive_broker_errors: int | None = None,
) -> PortfolioAllocationPlan:
    return PortfolioAllocationPlan(
        trading_date=date(2026, 1, 1),
        total_capital=200000.0,
        reserve_capital=0.0,
        intraday_capital=100000.0,
        max_daily_loss=max_daily_loss,
        max_trades_per_day=max_trades_per_day,
        max_capital_per_trade=max_capital_per_trade,
        max_open_positions=max_open_positions,
        max_symbol_allocation=(
            max_symbol_allocation if max_symbol_allocation is not None else max_capital_per_trade
        ),
        execution_mode="paper",
        max_consecutive_broker_errors=max_consecutive_broker_errors,
    )


def make_config(
    strategy_name: str,
    symbols: list[str],
    plan: PortfolioAllocationPlan | None = None,
    exit_policy: ExitPolicyConfig | None = None,
) -> TradingSessionConfig:
    return TradingSessionConfig(
        trading_date=date(2026, 1, 1),
        strategy_name=strategy_name,
        strategy_params={},
        execution_mode="paper",
        symbols=symbols,
        portfolio_allocation_plan=plan or make_plan(),
        exit_policy=exit_policy,
    )


def make_symbol_bars(entries: list[tuple[str, float]], exchange: str = "NSE") -> list[MarketBar]:
    base_time = datetime(2026, 1, 1, 9, 15, 0)
    return [
        MarketBar(
            symbol=symbol,
            exchange=exchange,
            timestamp=base_time + timedelta(minutes=i),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1000.0,
        )
        for i, (symbol, close) in enumerate(entries)
    ]


def make_orchestrator(
    bars: list[MarketBar], risk_manager: RiskManager, registry: StrategyRegistry, audit_sink=None
) -> tuple[ExecutionOrchestrator, PaperBroker]:
    market_data_service = MarketDataService()
    market_data_service.register_feed("test_feed", MemoryMarketDataFeed(bars=bars))
    broker = PaperBroker()
    orchestrator = ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=risk_manager,
        broker=broker,
        strategy_registry=registry,
        audit_sink=audit_sink,
    )
    return orchestrator, broker


class DisjointSymbolCoexistenceTests(unittest.TestCase):
    def test_two_strategies_on_different_symbols_both_trade_independently(self) -> None:
        bars = make_symbol_bars([("AAA", 100.0), ("BBB", 200.0)])
        registry = make_registry(("strat_a", AlwaysBuyStrategy), ("strat_b", AlwaysBuyStrategy))
        orchestrator, broker = make_orchestrator(bars, FakeRiskManager(True), registry)

        results = orchestrator.run_portfolio_session(
            [make_config("strat_a", ["AAA"]), make_config("strat_b", ["BBB"])]
        )

        self.assertEqual(set(results), {"strat_a", "strat_b"})
        self.assertEqual(results["strat_a"].approved_signals, 1)
        self.assertEqual(results["strat_b"].approved_signals, 1)
        self.assertEqual(results["strat_a"].placed_orders[0].symbol, "AAA")
        self.assertEqual(results["strat_b"].placed_orders[0].symbol, "BBB")

        final_symbols = {p.symbol for p in broker.list_positions()}
        self.assertEqual(final_symbols, {"AAA", "BBB"})


class SymbolContentionTests(unittest.TestCase):
    def test_first_strategy_in_config_order_wins_the_symbol(self) -> None:
        bars = make_symbol_bars([("TEST", 100.0)])
        registry = make_registry(("strat_a", AlwaysBuyStrategy), ("strat_b", AlwaysBuyStrategy))
        orchestrator, broker = make_orchestrator(bars, FakeRiskManager(True), registry)

        plan = make_plan(max_capital_per_trade=100.0)  # 1 share @ close=100
        results = orchestrator.run_portfolio_session(
            [make_config("strat_a", ["TEST"], plan=plan), make_config("strat_b", ["TEST"], plan=plan)]
        )

        self.assertEqual(results["strat_a"].approved_signals, 1)
        self.assertEqual(results["strat_b"].approved_signals, 0)
        self.assertEqual(results["strat_b"].rejected_signals, 1)

        # Not a blended position: exactly one position exists at all.
        positions = broker.list_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "TEST")
        self.assertEqual(positions[0].quantity, 1)

    def test_rejection_is_explicit_and_auditable_not_a_silent_skip(self) -> None:
        bars = make_symbol_bars([("TEST", 100.0)])
        registry = make_registry(("strat_a", AlwaysBuyStrategy), ("strat_b", AlwaysBuyStrategy))
        sink = InMemoryAuditSink()
        orchestrator, broker = make_orchestrator(bars, FakeRiskManager(True), registry, audit_sink=sink)

        orchestrator.run_portfolio_session(
            [make_config("strat_a", ["TEST"]), make_config("strat_b", ["TEST"])]
        )

        rejections = [
            e
            for e in sink.events_of_type(AuditEventType.SIGNAL_REJECTED)
            if e.strategy_name == "strat_b"
        ]
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0].payload["rejection_stage"], "symbol_owned_by_other_strategy")
        self.assertIn("strat_a", rejections[0].payload["reason"])


class PerStrategyRiskIsolationTests(unittest.TestCase):
    def test_one_strategys_daily_loss_latch_does_not_block_a_sibling_strategy(self) -> None:
        # strat_a: buys AAA at 100, stop_loss(10%) exits at 80 for a -20
        # realized loss, breaching its own max_daily_loss=15 - its rebuy
        # attempts on bars 2 and 3 must be blocked. strat_b trades BBB on
        # a separate, generous plan and must be entirely unaffected.
        bars = make_symbol_bars(
            [("AAA", 100.0), ("BBB", 100.0), ("AAA", 80.0), ("AAA", 80.0), ("BBB", 100.0)]
        )
        registry = make_registry(
            ("strat_a", ReBuyWhenFlatStrategy), ("strat_b", AlwaysBuyStrategy)
        )
        orchestrator, broker = make_orchestrator(bars, FakeRiskManager(True), registry)

        configs = [
            make_config(
                "strat_a",
                ["AAA"],
                plan=make_plan(max_capital_per_trade=100.0, max_daily_loss=15.0),
                exit_policy=ExitPolicyConfig(stop_loss_pct=10.0),
            ),
            make_config(
                "strat_b",
                ["BBB"],
                # max_symbol_allocation raised above max_capital_per_trade so
                # AlwaysBuyStrategy's repeated same-symbol accumulation isn't
                # blocked by the (unrelated) exposure guardrail - this test is
                # about the daily-loss guardrail's isolation, not exposure.
                plan=make_plan(max_capital_per_trade=100.0, max_symbol_allocation=10000.0),
            ),
        ]
        results = orchestrator.run_portfolio_session(configs)

        strat_a_state = results["strat_a"].final_session_risk_state
        self.assertTrue(strat_a_state.entry_blocked)
        self.assertEqual(strat_a_state.block_reason, "max_daily_loss_breached")
        self.assertEqual(results["strat_a"].approved_signals, 1)  # only the initial entry
        self.assertGreaterEqual(results["strat_a"].rejected_signals, 1)  # rebuy attempt(s) blocked
        self.assertEqual(len(results["strat_a"].exit_decisions), 1)
        self.assertEqual(results["strat_a"].exit_decisions[0].reason, "stop_loss")

        strat_b_state = results["strat_b"].final_session_risk_state
        self.assertFalse(strat_b_state.entry_blocked)
        self.assertEqual(results["strat_b"].approved_signals, 2)  # bars 1 and 4, unaffected


class SharedKillSwitchTests(unittest.TestCase):
    def test_one_strategys_broker_error_streak_flattens_every_strategys_positions(self) -> None:
        # strat_b successfully buys BBB first. strat_a's AAA bars always
        # raise; with max_consecutive_broker_errors=2 on strat_a's own
        # plan, the second AAA bar trips the shared kill switch, which
        # must flatten strat_b's BBB position too even though strat_b
        # never itself failed - a flaky shared connection threatens
        # everyone, per run_portfolio_session's docstring.
        bars = make_symbol_bars([("BBB", 100.0), ("AAA", 100.0), ("AAA", 100.0)])
        registry = make_registry(("strat_a", AlwaysFailsStrategy), ("strat_b", AlwaysBuyStrategy))
        orchestrator, broker = make_orchestrator(bars, FakeRiskManager(True), registry)

        configs = [
            make_config("strat_a", ["AAA"], plan=make_plan(max_consecutive_broker_errors=2)),
            make_config("strat_b", ["BBB"], plan=make_plan()),
        ]
        results = orchestrator.run_portfolio_session(configs)

        self.assertEqual(results["strat_b"].approved_signals, 1)  # bought once, before the trip
        self.assertTrue(results["strat_a"].final_session_risk_state.kill_switch_triggered)

        # Both strategies' positions were flattened - broker shows nothing open.
        self.assertEqual([p for p in broker.list_positions() if p.quantity != 0], [])


if __name__ == "__main__":
    unittest.main()

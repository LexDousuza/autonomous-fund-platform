"""
Unit tests for core.execution.orchestrator.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_execution_orchestrator.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from core.broker.base import OrderRequest, OrderSide
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
            )
        ]


class NeverSignalStrategy(Strategy):
    """Test strategy that never emits a signal."""

    def on_bar(self, bar, history, open_positions):
        return []


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
            reason="test_approved" if self._approve else "test_rejected",
        )


def make_bars(count: int, symbol: str = "TEST") -> list[MarketBar]:
    base_time = datetime(2026, 1, 1, 9, 15, 0)
    return [
        MarketBar(
            symbol=symbol,
            exchange="NSE",
            timestamp=base_time,
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
        max_capital_per_trade=15000.0,
        max_open_positions=4,
        max_symbol_allocation=15000.0,
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
    broker: PaperBroker | None = None,
) -> tuple[ExecutionOrchestrator, PaperBroker]:
    market_data_service = MarketDataService()
    market_data_service.register_feed("test_feed", MemoryMarketDataFeed(bars=bars))

    broker = broker or PaperBroker()
    orchestrator = ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=risk_manager,
        broker=broker,
        strategy_registry=make_registry(strategy_name, strategy_cls),
    )
    return orchestrator, broker


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


def make_config(
    strategy_name: str,
    symbols: list[str],
    quantity_per_trade: int = 1,
    exit_policy: ExitPolicyConfig | None = None,
) -> TradingSessionConfig:
    return TradingSessionConfig(
        trading_date=date(2026, 1, 1),
        strategy_name=strategy_name,
        strategy_params={},
        execution_mode="paper",
        symbols=symbols,
        portfolio_allocation_plan=make_allocation_plan(),
        quantity_per_trade=quantity_per_trade,
        exit_policy=exit_policy,
    )


class ExecutionOrchestratorTests(unittest.TestCase):
    def test_signal_approved_places_order(self) -> None:
        bars = make_bars(1)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config("always_buy", ["TEST"])

        result = orchestrator.run_session(config)

        self.assertEqual(result.processed_bars, 1)
        self.assertEqual(result.generated_signals, 1)
        self.assertEqual(result.approved_signals, 1)
        self.assertEqual(result.rejected_signals, 0)
        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(broker.list_positions(), result.final_positions)
        self.assertEqual(result.final_positions[0].symbol, "TEST")
        self.assertEqual(result.final_positions[0].quantity, 1)

    def test_signal_rejected_no_order_placed(self) -> None:
        bars = make_bars(1)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=False), "always_buy", AlwaysBuyStrategy
        )
        config = make_config("always_buy", ["TEST"])

        result = orchestrator.run_session(config)

        self.assertEqual(result.processed_bars, 1)
        self.assertEqual(result.generated_signals, 1)
        self.assertEqual(result.approved_signals, 0)
        self.assertEqual(result.rejected_signals, 1)
        self.assertEqual(result.placed_orders, [])
        self.assertEqual(broker.list_positions(), [])
        self.assertEqual(result.final_positions, [])

    def test_session_with_no_signals(self) -> None:
        bars = make_bars(3)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "never_signal", NeverSignalStrategy
        )
        config = make_config("never_signal", ["TEST"])

        result = orchestrator.run_session(config)

        self.assertEqual(result.processed_bars, 3)
        self.assertEqual(result.generated_signals, 0)
        self.assertEqual(result.approved_signals, 0)
        self.assertEqual(result.rejected_signals, 0)
        self.assertEqual(result.placed_orders, [])
        self.assertEqual(result.final_positions, [])

    def test_summary_counts_across_multiple_bars(self) -> None:
        bars = make_bars(3)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config("always_buy", ["TEST"], quantity_per_trade=2)

        result = orchestrator.run_session(config)

        self.assertEqual(result.processed_bars, 3)
        self.assertEqual(result.generated_signals, 3)
        self.assertEqual(result.approved_signals, 3)
        self.assertEqual(result.rejected_signals, 0)
        self.assertEqual(len(result.placed_orders), 3)
        # Same-direction accumulation: 3 buys of qty=2 each -> total qty 6
        self.assertEqual(result.final_positions[0].quantity, 6)

    def test_result_carries_trading_date_and_strategy_name(self) -> None:
        bars = make_bars(1)
        orchestrator, _ = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config("always_buy", ["TEST"])

        result = orchestrator.run_session(config)

        self.assertEqual(result.trading_date, config.trading_date)
        self.assertEqual(result.strategy_name, "always_buy")

    def test_only_subscribed_symbols_are_streamed(self) -> None:
        bars = make_bars(1, symbol="TEST") + make_bars(1, symbol="OTHER")
        orchestrator, _ = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config("always_buy", ["TEST"])

        result = orchestrator.run_session(config)

        self.assertEqual(result.processed_bars, 1)

    def test_entry_then_stop_loss_exit_closes_position(self) -> None:
        # Bar 0: entry at 100. Bar 1: price drops 10% -> stop loss (5%) fires.
        # Bar 2: nothing left to track, no further exit activity.
        bars = make_price_bars([100.0, 90.0, 90.0])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "buy_once", BuyOnceEverStrategy
        )
        config = make_config(
            "buy_once",
            ["TEST"],
            quantity_per_trade=1,
            exit_policy=ExitPolicyConfig(stop_loss_pct=5.0),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.processed_bars, 3)
        self.assertEqual(result.generated_signals, 1)
        self.assertEqual(result.approved_signals, 1)
        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(result.placed_orders[0].average_price, 100.0)

        self.assertEqual(len(result.exit_decisions), 1)
        self.assertEqual(result.exit_decisions[0].reason, "stop_loss")
        self.assertEqual(result.exit_decisions[0].quantity, 1)

        self.assertEqual(len(result.exit_orders), 1)
        self.assertEqual(result.exit_orders[0].side, OrderSide.SELL)
        self.assertEqual(result.exit_orders[0].average_price, 90.0)

        # Position closed: quantity back to zero after the exit fill.
        self.assertEqual(result.final_positions[0].quantity, 0)

    def test_target_exit_closes_position(self) -> None:
        bars = make_price_bars([100.0, 105.0])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "buy_once", BuyOnceEverStrategy
        )
        config = make_config(
            "buy_once",
            ["TEST"],
            quantity_per_trade=1,
            exit_policy=ExitPolicyConfig(target_pct=3.0),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(len(result.exit_decisions), 1)
        self.assertEqual(result.exit_decisions[0].reason, "target")
        self.assertEqual(result.final_positions[0].quantity, 0)

    def test_no_exit_when_policy_not_configured(self) -> None:
        bars = make_price_bars([100.0, 50.0])  # would trip a stop loss if one existed
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "buy_once", BuyOnceEverStrategy
        )
        config = make_config("buy_once", ["TEST"], quantity_per_trade=1)  # exit_policy=None

        result = orchestrator.run_session(config)

        self.assertEqual(result.exit_decisions, [])
        self.assertEqual(result.exit_orders, [])
        self.assertEqual(result.final_positions[0].quantity, 1)


if __name__ == "__main__":
    unittest.main()

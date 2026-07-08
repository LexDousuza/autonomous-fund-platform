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


class ReBuyWhenFlatStrategy(Strategy):
    """Test strategy that emits a BUY signal whenever it holds no open position."""

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


class PerSymbolHighBreakoutStrategy(Strategy):
    """
    Test strategy mirroring ``SimpleBreakoutMomentumStrategy`` with
    ``breakout_lookback_bars=1``: signals BUY only if the current close
    exceeds the high of the single most recent bar in ``history``.

    Used to prove ``history`` passed to a strategy is scoped to that
    bar's own symbol - if bars from other symbols leaked in, the "most
    recent bar" could belong to a different symbol entirely.
    """

    def on_bar(self, bar, history, open_positions):
        if not history:
            return []
        reference_high = history[-1].high
        if bar.close > reference_high:
            return [
                TradeSignal(
                    strategy_name=self.context.strategy_name,
                    symbol=bar.symbol,
                    exchange=bar.exchange,
                    side=SignalSide.BUY,
                    timestamp=bar.timestamp,
                    reason="test_per_symbol_breakout",
                )
            ]
        return []


class BuyOncePerSymbolStrategy(Strategy):
    """Test strategy that emits exactly one BUY per distinct symbol, ever (even if later flat)."""

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._bought_keys: set[str] = set()

    def on_bar(self, bar, history, open_positions):
        key = f"{bar.exchange}:{bar.symbol}"
        if key in self._bought_keys:
            return []
        self._bought_keys.add(key)
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_buy_once_per_symbol",
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


def make_allocation_plan(
    max_capital_per_trade: float = 15000.0,
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


def make_symbol_bars(entries: list[tuple[str, float]], exchange: str = "NSE") -> list[MarketBar]:
    """Build one bar per ``(symbol, close)`` entry, in the given order, each a minute apart."""
    base_time = datetime(2026, 1, 1, 9, 15, 0)
    return [
        MarketBar(
            symbol=symbol,
            exchange=exchange,
            timestamp=base_time.replace(minute=15 + i),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1000.0,
        )
        for i, (symbol, close) in enumerate(entries)
    ]


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


class ExecutionOrchestratorTests(unittest.TestCase):
    def test_signal_approved_places_order(self) -> None:
        bars = make_bars(1)  # bar close = 100.0
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        # max_capital_per_trade=100.0 at price=100.0 -> sizing approves qty=1
        config = make_config(
            "always_buy", ["TEST"], allocation_plan=make_allocation_plan(max_capital_per_trade=100.0)
        )

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
        bars = make_bars(3)  # bar closes = 100.0, 101.0, 102.0
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        # max_capital_per_trade=220.0 -> sizing approves qty=2 at each of these prices.
        # max_symbol_allocation is raised well above max_capital_per_trade so
        # repeated same-symbol accumulation isn't blocked by the exposure
        # guardrail; that guardrail has its own dedicated tests.
        config = make_config(
            "always_buy",
            ["TEST"],
            allocation_plan=make_allocation_plan(
                max_capital_per_trade=220.0, max_symbol_allocation=10000.0
            ),
        )

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
            allocation_plan=make_allocation_plan(max_capital_per_trade=100.0),  # qty=1 at price=100
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
            allocation_plan=make_allocation_plan(max_capital_per_trade=100.0),  # qty=1 at price=100
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
        config = make_config(
            "buy_once", ["TEST"], allocation_plan=make_allocation_plan(max_capital_per_trade=100.0)
        )  # exit_policy=None

        result = orchestrator.run_session(config)

        self.assertEqual(result.exit_decisions, [])
        self.assertEqual(result.exit_orders, [])
        self.assertEqual(result.final_positions[0].quantity, 1)

    def test_placed_order_quantity_comes_from_sizing_engine(self) -> None:
        # bar close = 100.0, max_capital_per_trade = 15000.0 -> floor(15000/2500) = 6
        bars = make_price_bars([2500.0])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config(
            "always_buy", ["TEST"], allocation_plan=make_allocation_plan(max_capital_per_trade=15000.0)
        )

        result = orchestrator.run_session(config)

        self.assertEqual(len(result.sizing_decisions), 1)
        self.assertEqual(result.sizing_decisions[0].approved_quantity, 6)
        self.assertEqual(result.sizing_decisions[0].capital_used, 15000.0)
        self.assertEqual(result.sizing_decisions[0].reason, "approved")

        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(result.placed_orders[0].quantity, 6)
        self.assertEqual(result.final_positions[0].quantity, 6)

    def test_signal_rejected_when_sizing_yields_zero_quantity(self) -> None:
        # price=150.0 cannot afford even 1 share of a 100.0 capital budget
        bars = make_price_bars([150.0])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config(
            "always_buy", ["TEST"], allocation_plan=make_allocation_plan(max_capital_per_trade=100.0)
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.generated_signals, 1)
        self.assertEqual(result.approved_signals, 0)
        self.assertEqual(result.rejected_signals, 1)
        self.assertEqual(result.placed_orders, [])
        self.assertEqual(broker.list_positions(), [])

        self.assertEqual(len(result.sizing_decisions), 1)
        self.assertEqual(result.sizing_decisions[0].approved_quantity, 0)
        self.assertEqual(result.sizing_decisions[0].reason, "price_exceeds_capital_per_trade")

    def test_no_guardrail_block_before_thresholds_reached(self) -> None:
        bars = make_price_bars([100.0, 100.0, 100.0])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config(
            "always_buy",
            ["TEST"],
            allocation_plan=make_allocation_plan(
                max_capital_per_trade=100.0,
                max_trades_per_day=10,
                max_daily_loss=100000.0,
                max_symbol_allocation=10000.0,
            ),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.skipped_due_to_session_guardrails, 0)
        self.assertEqual(result.approved_signals, 3)
        self.assertIsNotNone(result.final_session_risk_state)
        self.assertFalse(result.final_session_risk_state.entry_blocked)
        self.assertIsNone(result.final_session_risk_state.block_reason)

    def test_guardrail_blocks_after_max_trades_per_day_reached(self) -> None:
        bars = make_price_bars([100.0] * 5)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config(
            "always_buy",
            ["TEST"],
            allocation_plan=make_allocation_plan(
                max_capital_per_trade=100.0, max_trades_per_day=2, max_symbol_allocation=10000.0
            ),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.generated_signals, 5)
        self.assertEqual(result.approved_signals, 2)
        self.assertEqual(result.rejected_signals, 3)
        self.assertEqual(result.skipped_due_to_session_guardrails, 3)
        self.assertEqual(len(result.placed_orders), 2)

        # Same-direction accumulation: 2 buys of qty=1 each -> total qty 2
        self.assertEqual(result.final_positions[0].quantity, 2)

        state = result.final_session_risk_state
        self.assertTrue(state.entry_blocked)
        self.assertEqual(state.block_reason, "max_trades_per_day_reached")
        self.assertEqual(state.entries_taken, 2)

    def test_guardrail_blocks_after_max_daily_loss_breached(self) -> None:
        # Bar 0 (100): flat -> buy. Bar 1 (80): stop loss (10%) exits at a
        # -20 realized loss, which alone breaches a -15 daily loss floor,
        # latching the block before the same bar's re-entry attempt.
        bars = make_price_bars([100.0, 80.0, 100.0, 80.0])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "rebuy", ReBuyWhenFlatStrategy
        )
        config = make_config(
            "rebuy",
            ["TEST"],
            allocation_plan=make_allocation_plan(max_capital_per_trade=100.0, max_daily_loss=15.0),
            exit_policy=ExitPolicyConfig(stop_loss_pct=10.0),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.processed_bars, 4)
        self.assertEqual(result.approved_signals, 1)  # only the bar-0 entry
        self.assertEqual(result.skipped_due_to_session_guardrails, 3)  # bars 1, 2, 3

        self.assertEqual(len(result.exit_decisions), 1)
        self.assertEqual(result.exit_decisions[0].reason, "stop_loss")

        state = result.final_session_risk_state
        self.assertTrue(state.entry_blocked)
        self.assertEqual(state.block_reason, "max_daily_loss_breached")
        self.assertEqual(state.realized_pnl, -20.0)

        # No re-entry after the exit: guardrails blocked it.
        self.assertEqual(result.final_positions[0].quantity, 0)

    def test_exits_still_processed_after_entries_blocked(self) -> None:
        # Bar 0: entry at 100, immediately latches max_trades_per_day=1.
        # Bar 1: price drops 10% -> stop loss must still fire even though
        # new entries are blocked.
        bars = make_price_bars([100.0, 90.0])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "buy_once", BuyOnceEverStrategy
        )
        config = make_config(
            "buy_once",
            ["TEST"],
            allocation_plan=make_allocation_plan(max_capital_per_trade=100.0, max_trades_per_day=1),
            exit_policy=ExitPolicyConfig(stop_loss_pct=5.0),
        )

        result = orchestrator.run_session(config)

        state = result.final_session_risk_state
        self.assertTrue(state.entry_blocked)
        self.assertEqual(state.block_reason, "max_trades_per_day_reached")

        self.assertEqual(len(result.exit_decisions), 1)
        self.assertEqual(result.exit_decisions[0].reason, "stop_loss")
        self.assertEqual(len(result.exit_orders), 1)
        self.assertEqual(result.final_positions[0].quantity, 0)

    def test_multi_symbol_session_tracks_independent_positions(self) -> None:
        bars = make_symbol_bars([("AAA", 100.0), ("BBB", 200.0)])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config(
            "always_buy",
            ["AAA", "BBB"],
            allocation_plan=make_allocation_plan(max_capital_per_trade=200.0, max_open_positions=4),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.processed_bars, 2)
        self.assertEqual(result.generated_signals, 2)
        self.assertEqual(result.approved_signals, 2)
        self.assertEqual(result.symbols_with_signals, {"AAA", "BBB"})

        placed_symbols = {order.symbol for order in result.placed_orders}
        self.assertEqual(placed_symbols, {"AAA", "BBB"})

        # max_capital_per_trade=200.0 -> floor(200/100)=2 for AAA, floor(200/200)=1 for BBB
        final_by_symbol = {p.symbol: p.quantity for p in result.final_positions}
        self.assertEqual(final_by_symbol, {"AAA": 2, "BBB": 1})

    def test_per_symbol_history_is_not_shared_across_symbols(self) -> None:
        # Interleaved: A0, B0, A1, B1. A1's close (150) only breaks A's own
        # bar-0 high (100), not B's bar-0 high (500). If `history` leaked
        # across symbols, A1 would incorrectly compare against B0 and fail
        # to signal.
        bars = make_symbol_bars(
            [("AAA", 90.0), ("BBB", 490.0), ("AAA", 150.0), ("BBB", 510.0)]
        )
        # Bar highs equal closes from make_symbol_bars only for the first
        # occurrence per symbol in this construction; set highs explicitly.
        bars[0].high = 100.0
        bars[1].high = 500.0

        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "breakout", PerSymbolHighBreakoutStrategy
        )
        config = make_config(
            "breakout",
            ["AAA", "BBB"],
            allocation_plan=make_allocation_plan(max_capital_per_trade=1000.0, max_open_positions=4),
        )

        result = orchestrator.run_session(config)

        # Both A1 (150 > A's own high of 100) and B1 (510 > B's own high of
        # 500) should signal once history is correctly scoped per symbol.
        self.assertEqual(result.generated_signals, 2)
        self.assertEqual(result.symbols_with_signals, {"AAA", "BBB"})
        self.assertEqual(result.approved_signals, 2)

    def test_max_open_positions_blocks_new_symbol_entry(self) -> None:
        bars = make_symbol_bars([("AAA", 100.0), ("BBB", 100.0), ("CCC", 100.0)])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config(
            "always_buy",
            ["AAA", "BBB", "CCC"],
            allocation_plan=make_allocation_plan(
                max_capital_per_trade=100.0, max_open_positions=2, max_symbol_allocation=10000.0
            ),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.generated_signals, 3)
        self.assertEqual(result.approved_signals, 2)
        self.assertEqual(result.rejected_signals, 1)
        self.assertEqual(result.skipped_due_to_session_guardrails, 0)
        self.assertEqual(result.skipped_due_to_position_limits, 1)

        placed_symbols = {order.symbol for order in result.placed_orders}
        self.assertEqual(placed_symbols, {"AAA", "BBB"})

        final_symbols = {p.symbol for p in result.final_positions}
        self.assertEqual(final_symbols, {"AAA", "BBB"})

    def test_symbol_allocation_rejects_oversized_addition(self) -> None:
        # Same symbol, twice: first entry uses the full 100.0 budget.
        # A second entry would push cumulative notional to 200.0, over a
        # 150.0 max_symbol_allocation ceiling.
        bars = make_price_bars([100.0, 100.0])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config(
            "always_buy",
            ["TEST"],
            allocation_plan=make_allocation_plan(
                max_capital_per_trade=100.0, max_open_positions=4, max_symbol_allocation=150.0
            ),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.approved_signals, 1)
        self.assertEqual(result.skipped_due_to_position_limits, 1)
        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(result.final_positions[0].quantity, 1)

    def test_exits_continue_when_max_open_positions_reached(self) -> None:
        # Bar 0: AAA opens, filling the only open-position slot. Bar 1:
        # BBB's entry attempt is blocked (AAA still occupies the slot).
        # Bar 2: AAA's stop-loss exit must still fire even though entries
        # are exposure-blocked.
        bars = make_symbol_bars([("AAA", 100.0), ("BBB", 100.0), ("AAA", 90.0)])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "buy_once_per_symbol", BuyOncePerSymbolStrategy
        )
        config = make_config(
            "buy_once_per_symbol",
            ["AAA", "BBB"],
            allocation_plan=make_allocation_plan(
                max_capital_per_trade=100.0, max_open_positions=1, max_symbol_allocation=10000.0
            ),
            exit_policy=ExitPolicyConfig(stop_loss_pct=5.0),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.approved_signals, 1)  # AAA only
        self.assertEqual(result.skipped_due_to_position_limits, 1)  # BBB blocked

        self.assertEqual(len(result.exit_decisions), 1)
        self.assertEqual(result.exit_decisions[0].reason, "stop_loss")
        self.assertEqual(result.exit_decisions[0].symbol, "AAA")

        aaa_position = next(p for p in result.final_positions if p.symbol == "AAA")
        self.assertEqual(aaa_position.quantity, 0)
        self.assertNotIn("BBB", [p.symbol for p in result.final_positions])


if __name__ == "__main__":
    unittest.main()

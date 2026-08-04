"""
Unit tests for core.execution.orchestrator.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_execution_orchestrator.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta

from core.broker.base import OptionType, OrderRequest, OrderSide, PositionEffect
from core.broker.estimated_margin import EstimatedSpanMarginCalculator
from core.broker.paper import PaperBroker
from core.execution.models import TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator, build_exit_order, build_order_from_signal
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.portfolio.exit_models import ExitDecision, ExitPolicyConfig
from core.portfolio.models import PortfolioAllocationPlan
from core.portfolio.position_manager import PositionManager
from core.risk.base import RiskContext, RiskDecision, RiskManager
from core.risk.options_margin import OptionsMarginRiskManager
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


class HistoryReportingStrategy(Strategy):
    """Test strategy that records len(history) on the class itself (no signals) - for
    asserting what history a caller's pre-seeded history_by_symbol actually delivers."""

    last_history_length: int = 0

    def on_bar(self, bar, history, open_positions):
        HistoryReportingStrategy.last_history_length = len(history)
        return []


class DoubleSignalStrategy(Strategy):
    """Test strategy simulating a bug: emits the same BUY signal twice for one bar."""

    def on_bar(self, bar, history, open_positions):
        signal = TradeSignal(
            strategy_name=self.context.strategy_name,
            symbol=bar.symbol,
            exchange=bar.exchange,
            side=SignalSide.BUY,
            timestamp=bar.timestamp,
            reason="test_double_signal",
        )
        return [signal, signal]


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


class SellToOpenOnceStrategy(Strategy):
    """Test strategy that emits exactly one naked-write-open SELL signal, on the first bar it sees."""

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._sold = False

    def on_bar(self, bar, history, open_positions):
        if self._sold:
            return []
        self._sold = True
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.SELL,
                timestamp=bar.timestamp,
                reason="test_sell_to_open_once",
                metadata={"market_price": bar.close},
                option_type=OptionType.CALL,
                strike=100.0,
                expiry="2026-08-27",
                lot_size=1,
                position_effect=PositionEffect.OPEN,
            )
        ]


class AlwaysSellToOpenStrategy(Strategy):
    """Test strategy that emits a naked-write-open SELL signal on every bar, unconditionally."""

    def on_bar(self, bar, history, open_positions):
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.SELL,
                timestamp=bar.timestamp,
                reason="test_always_sell_to_open",
                metadata={"market_price": bar.close},
                option_type=OptionType.CALL,
                strike=100.0,
                expiry="2026-08-27",
                lot_size=1,
                position_effect=PositionEffect.OPEN,
            )
        ]


class SellToOpenDifferentSymbolStrategy(Strategy):
    """
    Test strategy that emits exactly one naked-write-open SELL signal, on
    the first bar it sees, for a DIFFERENT symbol than the bar that
    triggered it (e.g. an underlying's bar triggering a signal on that
    underlying's own option contract) - unlike SellToOpenOnceStrategy
    above, which (like every other single-symbol test in this file)
    happens to write on the *same* symbol as the triggering bar, silently
    sidestepping the real-world case where the option symbol was never
    part of the session's originally-subscribed universe at all.
    """

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._sold = False

    def on_bar(self, bar, history, open_positions):
        if self._sold:
            return []
        self._sold = True
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol="NIFTY_PE_18000",
                exchange="NFO",
                side=SignalSide.SELL,
                timestamp=bar.timestamp,
                reason="test_sell_to_open_different_symbol",
                metadata={"market_price": 50.0},
                option_type=OptionType.PUT,
                strike=18000.0,
                expiry="2026-08-27",
                lot_size=1,
                position_effect=PositionEffect.OPEN,
            )
        ]


class OnceOptionsSignalStrategy(Strategy):
    """
    Test strategy that emits exactly one options BUY/OPEN signal, on the
    first bar it sees, with a configurable ``market_price`` metadata
    value (or none at all) - used to prove the orchestrator prices/sizes
    an options signal off this value, never off bar.close.
    """

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._signaled = False

    def on_bar(self, bar, history, open_positions):
        if self._signaled:
            return []
        self._signaled = True
        params = self.context.parameters or {}
        metadata = {}
        if "market_price" in params:
            metadata["market_price"] = params["market_price"]
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_options_signal",
                metadata=metadata,
                option_type=OptionType.CALL,
                strike=100.0,
                expiry="2026-08-27",
                lot_size=1,
                position_effect=PositionEffect.OPEN,
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
            timestamp=base_time + timedelta(minutes=i),  # distinct per bar, like real market data
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
    available_margin: float | None = None,
) -> TradingSessionConfig:
    return TradingSessionConfig(
        trading_date=date(2026, 1, 1),
        strategy_name=strategy_name,
        strategy_params={},
        execution_mode="paper",
        symbols=symbols,
        portfolio_allocation_plan=allocation_plan or make_allocation_plan(),
        exit_policy=exit_policy,
        available_margin=available_margin,
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

    def test_duplicate_signal_for_the_same_bar_places_only_one_order(self) -> None:
        # Simulates a strategy bug that emits the "same" BUY signal twice
        # for one bar: the idempotency guard must block the second one
        # rather than double-entering the position.
        bars = make_bars(1)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "double_signal", DoubleSignalStrategy
        )
        config = make_config(
            "double_signal", ["TEST"], allocation_plan=make_allocation_plan(max_capital_per_trade=100.0)
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.generated_signals, 2)
        self.assertEqual(result.approved_signals, 1)
        self.assertEqual(result.rejected_signals, 0)
        self.assertEqual(result.duplicate_orders_blocked, 1)
        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(result.final_positions[0].quantity, 1)

    def test_orchestrator_sets_a_client_order_id_on_placed_orders(self) -> None:
        # White-box: PaperBroker only ever populates _orders_by_client_id
        # when place_order() receives an OrderRequest with client_order_id
        # set, so a non-empty map here proves the orchestrator is setting
        # it (see build_order_from_signal's client_order_id parameter).
        bars = make_bars(1)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config(
            "always_buy", ["TEST"], allocation_plan=make_allocation_plan(max_capital_per_trade=100.0)
        )

        result = orchestrator.run_session(config)

        self.assertEqual(len(broker._orders_by_client_id), 1)
        (client_order_id, order_id), = broker._orders_by_client_id.items()
        self.assertEqual(order_id, result.placed_orders[0].order_id)
        self.assertTrue(client_order_id)

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


class BuildOrderFromSignalOptionsFieldsTests(unittest.TestCase):
    def test_cash_equity_signal_leaves_options_fields_at_defaults(self) -> None:
        signal = TradeSignal(
            strategy_name="test",
            symbol="RELIANCE",
            exchange="NSE",
            side=SignalSide.BUY,
            timestamp=datetime(2026, 1, 1, 9, 15),
            reason="test",
        )

        order = build_order_from_signal(signal, quantity=10, price=2500.0)

        self.assertIsNone(order.strike)
        self.assertIsNone(order.expiry)
        self.assertIsNone(order.option_type)
        self.assertEqual(order.lot_size, 1)
        self.assertIsNone(order.position_effect)

    def test_options_signal_fields_are_threaded_through(self) -> None:
        signal = TradeSignal(
            strategy_name="test",
            symbol="RELIANCE26AUG2600CE",
            exchange="NFO",
            side=SignalSide.SELL,
            timestamp=datetime(2026, 1, 1, 9, 15),
            reason="test",
            strike=2600.0,
            expiry="2026-08-27",
            option_type=OptionType.CALL,
            lot_size=250,
            position_effect=PositionEffect.OPEN,
        )

        order = build_order_from_signal(signal, quantity=250, price=15.0)

        self.assertEqual(order.strike, 2600.0)
        self.assertEqual(order.expiry, "2026-08-27")
        self.assertEqual(order.option_type, OptionType.CALL)
        self.assertEqual(order.lot_size, 250)
        self.assertEqual(order.position_effect, PositionEffect.OPEN)


class BuildExitOrderShortAwareSideTests(unittest.TestCase):
    def test_long_position_exit_is_sell_of_full_quantity(self) -> None:
        decision = ExitDecision(
            should_exit=True, symbol="RELIANCE", exchange="NSE", quantity=10, reason="stop_loss", exit_price=90.0
        )

        order = build_exit_order(decision, strategy_name="test")

        self.assertEqual(order.side, OrderSide.SELL)
        self.assertEqual(order.quantity, 10)

    def test_short_position_exit_is_buy_to_cover_of_full_quantity(self) -> None:
        decision = ExitDecision(
            should_exit=True, symbol="RELIANCE26AUG2600CE", exchange="NFO", quantity=-250,
            reason="stop_loss", exit_price=18.0,
        )

        order = build_exit_order(decision, strategy_name="test")

        self.assertEqual(order.side, OrderSide.BUY)
        self.assertEqual(order.quantity, 250)  # magnitude, not the signed tracked quantity


class NakedWriteOpenOrchestratorTests(unittest.TestCase):
    """
    End-to-end proof that a SELL+OPEN signal registers a negative-quantity
    tracked position, and that exits.py's short-aware stop/target math plus
    build_exit_order's buy-to-cover side selection all interoperate.
    """

    def test_sell_to_open_then_price_rise_exits_via_buy_to_cover(self) -> None:
        # Bar 0: sell-to-open at 100. Bar 1: price rises 10% -> for a
        # short, that's a loss, so the 5% stop_loss must fire (mirror of
        # the long case, where a *falling* price triggers the stop).
        bars = make_price_bars([100.0, 110.0, 110.0])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "sell_to_open_once", SellToOpenOnceStrategy
        )
        config = make_config(
            "sell_to_open_once",
            ["TEST"],
            allocation_plan=make_allocation_plan(max_capital_per_trade=100.0),  # qty=1 at price=100
            exit_policy=ExitPolicyConfig(stop_loss_pct=5.0),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(result.placed_orders[0].side, OrderSide.SELL)

        self.assertEqual(len(result.exit_decisions), 1)
        self.assertEqual(result.exit_decisions[0].reason, "stop_loss")
        self.assertEqual(result.exit_decisions[0].quantity, -1)  # tracked as a negative (short) position

        self.assertEqual(len(result.exit_orders), 1)
        self.assertEqual(result.exit_orders[0].side, OrderSide.BUY)  # buy-to-cover, not sell
        self.assertEqual(result.exit_orders[0].quantity, 1)

        # Position closed: quantity back to zero after the buy-to-cover fill.
        self.assertEqual(result.final_positions[0].quantity, 0)

    def test_naked_write_on_a_symbol_never_in_config_symbols_still_exits_at_square_off(self) -> None:
        # The real-world shape this platform actually hits: the session
        # only ever subscribes the underlying ("NIFTY"); the option symbol
        # ("NIFTY_PE_18000") is discovered dynamically, at signal time, and
        # was never part of config.symbols. Before the memory-feed fix,
        # MemoryMarketDataFeed has zero bars for a symbol it was never
        # constructed with, so this position could never be evaluated for
        # exit again - it would still show fully open at session end
        # despite square_off_time having clearly passed.
        bars = make_symbol_bars([("NIFTY", 100.0), ("NIFTY", 101.0), ("NIFTY", 102.0)])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "sell_to_open_diff", SellToOpenDifferentSymbolStrategy
        )
        config = make_config(
            "sell_to_open_diff",
            ["NIFTY"],
            allocation_plan=make_allocation_plan(max_capital_per_trade=100.0),
            exit_policy=ExitPolicyConfig(square_off_time=time(9, 16)),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(result.placed_orders[0].symbol, "NIFTY_PE_18000")

        self.assertEqual(len(result.exit_decisions), 1)
        self.assertEqual(result.exit_decisions[0].symbol, "NIFTY_PE_18000")
        self.assertEqual(result.exit_decisions[0].reason, "square_off")

        option_position = next(p for p in result.final_positions if p.symbol == "NIFTY_PE_18000")
        self.assertEqual(option_position.quantity, 0)


class NakedWriteMarginTrackingTests(unittest.TestCase):
    """
    A session's ``available_margin`` is fetched once (see
    ``TradingSessionConfig.available_margin``'s docstring) but must still
    reflect margin this same session already committed to an earlier
    naked write - otherwise a second naked write is checked against the
    same static figure the first one already consumed from.
    """

    def test_second_naked_write_in_session_is_rejected_once_cumulative_margin_exhausts_available(
        self,
    ) -> None:
        # Two different underlyings so exposure/position guardrails never
        # enter into it - this isolates the margin-tracking bug alone.
        # EstimatedSpanMarginCalculator: required_margin = strike * qty * 0.20
        # = 100 * 1 * 0.20 = 20.0 per naked write (qty=1 from
        # max_capital_per_trade=100.0 at price=100.0).
        bars = make_symbol_bars([("AAA", 100.0), ("BBB", 100.0)])
        risk_manager = OptionsMarginRiskManager(
            FakeRiskManager(approve=True), margin_calculator=EstimatedSpanMarginCalculator()
        )
        orchestrator, broker = make_orchestrator(
            bars, risk_manager, "always_sell_to_open", AlwaysSellToOpenStrategy
        )
        config = make_config(
            "always_sell_to_open",
            ["AAA", "BBB"],
            allocation_plan=make_allocation_plan(max_capital_per_trade=100.0),
            available_margin=30.0,  # affords exactly one 20.0 write, not two
        )

        result = orchestrator.run_session(config)

        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(result.placed_orders[0].symbol, "AAA")
        self.assertEqual(result.rejected_signals, 1)

    def test_naked_write_approved_when_cumulative_margin_still_fits(self) -> None:
        # Same setup but available_margin=50.0 comfortably covers two
        # 20.0 writes (40.0 total) - proves the fix doesn't over-reject.
        bars = make_symbol_bars([("AAA", 100.0), ("BBB", 100.0)])
        risk_manager = OptionsMarginRiskManager(
            FakeRiskManager(approve=True), margin_calculator=EstimatedSpanMarginCalculator()
        )
        orchestrator, broker = make_orchestrator(
            bars, risk_manager, "always_sell_to_open", AlwaysSellToOpenStrategy
        )
        config = make_config(
            "always_sell_to_open",
            ["AAA", "BBB"],
            allocation_plan=make_allocation_plan(max_capital_per_trade=100.0),
            available_margin=50.0,
        )

        result = orchestrator.run_session(config)

        self.assertEqual(len(result.placed_orders), 2)
        self.assertEqual(result.rejected_signals, 0)


class OptionsSignalPricingTests(unittest.TestCase):
    """
    An options signal (option_type set) must be priced/sized off
    signal.metadata["market_price"], never bar.close - see the
    orchestrator's comment at the sizing_request/build_order_from_signal
    call sites for why (only the underlying is subscribed pre-signal, so
    bar.close is the underlying's spot price, not the option's).
    """

    def _make_config_with_params(
        self, strategy_name: str, symbols: list[str], strategy_params: dict, allocation_plan
    ) -> TradingSessionConfig:
        return TradingSessionConfig(
            trading_date=date(2026, 1, 1),
            strategy_name=strategy_name,
            strategy_params=strategy_params,
            execution_mode="paper",
            symbols=symbols,
            portfolio_allocation_plan=allocation_plan,
        )

    def test_sizes_and_prices_off_metadata_market_price_not_bar_close(self) -> None:
        # bar.close is 100.0 (see make_bars); market_price is deliberately
        # very different (20.0, a plausible option premium vs. a much
        # higher underlying spot) so the two can never be confused.
        bars = make_bars(1)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "options_signal", OnceOptionsSignalStrategy
        )
        # max_capital_per_trade=20.0 only affords qty=1 at price=20.0 (the
        # metadata price) - at bar.close=100.0 it would afford qty=0,
        # rejecting the signal entirely, so this also proves the price
        # actually used by asserting a fill happens at all.
        config = self._make_config_with_params(
            "options_signal",
            ["TEST"],
            strategy_params={"market_price": 20.0},
            allocation_plan=make_allocation_plan(max_capital_per_trade=20.0),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.rejected_signals, 0)
        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(result.placed_orders[0].average_price, 20.0)

    def test_missing_market_price_is_rejected_not_silently_priced_at_bar_close(self) -> None:
        bars = make_bars(1)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "options_signal", OnceOptionsSignalStrategy
        )
        config = self._make_config_with_params(
            "options_signal",
            ["TEST"],
            strategy_params={},  # no "market_price" key at all
            allocation_plan=make_allocation_plan(max_capital_per_trade=100.0),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.rejected_signals, 1)
        self.assertEqual(len(result.placed_orders), 0)

    def test_non_positive_market_price_is_rejected(self) -> None:
        bars = make_bars(1)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "options_signal", OnceOptionsSignalStrategy
        )
        config = self._make_config_with_params(
            "options_signal",
            ["TEST"],
            strategy_params={"market_price": 0.0},
            allocation_plan=make_allocation_plan(max_capital_per_trade=100.0),
        )

        result = orchestrator.run_session(config)

        self.assertEqual(result.rejected_signals, 1)
        self.assertEqual(len(result.placed_orders), 0)

    def test_cash_equity_signal_still_prices_off_bar_close(self) -> None:
        # Regression guard: the fix must not touch the far more common
        # cash-equity path at all.
        bars = make_bars(1)  # bar.close = 100.0
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config(
            "always_buy", ["TEST"], allocation_plan=make_allocation_plan(max_capital_per_trade=100.0)
        )

        result = orchestrator.run_session(config)

        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(result.placed_orders[0].average_price, 100.0)


class BuyNewSymbolOnceStrategy(Strategy):
    """Buys a symbol NOT in config.symbols on the very first bar, then never signals again."""

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
                symbol="TEST_OPT", exchange="NSE", side=SignalSide.BUY, timestamp=bar.timestamp,
                reason="test_buy_new_symbol",
            )
        ]


class BuyOfSymbolOutsideConfigIsStillExitEvaluatedTests(unittest.TestCase):
    """
    core.execution.signal_pipeline.SignalPipeline.process_signal's SELL+OPEN
    branch already subscribes/seeds a naked-write's dynamically-chosen
    option symbol (see its own comment on why: a live Kite/Groww feed
    only polls what's subscribed, and MemoryMarketDataFeed's bars are a
    fixed list built before that symbol existed) - the plain BUY branch
    never did the same. Invisible for every existing strategy (a BUY
    always targets a symbol already in config.symbols), but exactly what
    core.strategy.credit_spread's protective long leg does: buys a
    dynamically-chosen option contract never in config.symbols. Without
    this, that leg would be tracked but never exit-evaluated again, in
    live/paper too, not just a backtest - masked until now only because
    credit_spread was hard-blocked from ever running at all.
    """

    def test_bought_symbol_not_in_config_symbols_is_still_exit_evaluated(self) -> None:
        base = datetime(2026, 1, 1, 9, 15, 0)
        trigger_bar = MarketBar(
            symbol="TEST", exchange="NSE", timestamp=base,
            open=100.0, high=100.0, low=100.0, close=100.0, volume=1000.0,
        )
        # A later bar for the BOUGHT symbol, breaching a 5% stop - only
        # ever streams at all if the buy correctly subscribed it (see
        # MemoryMarketDataFeed.stream()'s subscription filter).
        later_bar = MarketBar(
            symbol="TEST_OPT", exchange="NSE", timestamp=base + timedelta(minutes=1),
            open=80.0, high=80.0, low=80.0, close=80.0, volume=1000.0,
        )
        orchestrator, broker = make_orchestrator(
            [trigger_bar, later_bar], FakeRiskManager(approve=True), "buy_new_symbol", BuyNewSymbolOnceStrategy
        )
        config = make_config("buy_new_symbol", ["TEST"], exit_policy=ExitPolicyConfig(stop_loss_pct=5.0))

        result = orchestrator.run_session(config)

        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(len(result.exit_orders), 1)
        self.assertEqual(result.exit_decisions[0].reason, "stop_loss")

    def test_ordinary_buy_of_an_already_subscribed_symbol_is_unaffected(self) -> None:
        # Regression guard: every existing strategy buys a symbol already
        # in config.symbols - subscribe()/seed_synthetic_price_path are
        # both documented no-ops for an already-subscribed/already-real
        # symbol, so this must behave exactly as before the fix.
        bars = make_price_bars([100.0, 80.0])  # bar 2 breaches a 5% stop
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        config = make_config("always_buy", ["TEST"], exit_policy=ExitPolicyConfig(stop_loss_pct=5.0))

        result = orchestrator.run_session(config)

        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(len(result.exit_orders), 1)
        self.assertEqual(result.exit_decisions[0].reason, "stop_loss")


class BuyWithGroupIdStrategy(Strategy):
    """Emits exactly one BUY signal, tagged with a fixed group_id, on the first bar it sees."""

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._signaled = False

    def on_bar(self, bar, history, open_positions):
        if self._signaled:
            return []
        self._signaled = True
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol, exchange=bar.exchange, side=SignalSide.BUY, timestamp=bar.timestamp,
                reason="test_buy_with_group_id", group_id="group-xyz",
            )
        ]


class SellToOpenWithGroupIdStrategy(Strategy):
    """Emits exactly one naked-write SELL+OPEN signal, tagged with a fixed group_id."""

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._signaled = False

    def on_bar(self, bar, history, open_positions):
        if self._signaled:
            return []
        self._signaled = True
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol, exchange=bar.exchange, side=SignalSide.SELL, timestamp=bar.timestamp,
                reason="test_sell_to_open_with_group_id", metadata={"market_price": 50.0},
                option_type=OptionType.PUT, strike=18000.0, expiry="2026-08-27", lot_size=1,
                position_effect=PositionEffect.OPEN, group_id="group-xyz",
            )
        ]


class SignalGroupIdThreadingTests(unittest.TestCase):
    """
    TradeSignal.group_id (see core/strategy/base.py) is how a strategy
    emitting multiple legs from one signal chain - e.g. credit_spread's
    long+short pair - tags them as belonging together, so
    PositionManager.evaluate_group_exits can later evaluate and close
    them as one joint decision. Threaded through both branches of
    SignalPipeline.process_signal that call position_manager.register_entry.
    """

    def test_buy_signal_group_id_is_threaded_to_the_tracked_position(self) -> None:
        bars = make_bars(1)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "buy_with_group", BuyWithGroupIdStrategy
        )
        position_manager = PositionManager()

        orchestrator.run_session(make_config("buy_with_group", ["TEST"]), position_manager=position_manager)

        self.assertEqual(position_manager.open_positions()["NSE:TEST"].group_id, "group-xyz")

    def test_sell_to_open_signal_group_id_is_threaded_to_the_tracked_position(self) -> None:
        bars = make_bars(1)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "sell_open_with_group", SellToOpenWithGroupIdStrategy
        )
        position_manager = PositionManager()

        orchestrator.run_session(
            make_config("sell_open_with_group", ["TEST"]), position_manager=position_manager
        )

        self.assertEqual(position_manager.open_positions()["NSE:TEST"].group_id, "group-xyz")

    def test_signal_without_group_id_produces_none_unchanged(self) -> None:
        bars = make_bars(1)
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "always_buy", AlwaysBuyStrategy
        )
        position_manager = PositionManager()

        orchestrator.run_session(make_config("always_buy", ["TEST"]), position_manager=position_manager)

        self.assertIsNone(position_manager.open_positions()["NSE:TEST"].group_id)


class RunSessionCarriedPositionManagerTests(unittest.TestCase):
    """
    run_session's optional position_manager= kwarg (see
    core/backtest/multi_day_position_runner.py, the caller that actually
    needs this): a caller carrying a position across a simulated day
    boundary passes an already-populated PositionManager in instead of
    letting run_session build an empty one - the seam that makes
    multi-day holding possible without duplicating run_session's
    guardrail/pricing/sizing/exposure/risk/place/register pipeline.
    """

    def test_carried_position_is_exit_evaluated_without_a_new_entry(self) -> None:
        # A position opened "yesterday" - this session's own strategy
        # never signals at all - must still be exit-evaluated from bar
        # one purely because it was passed in via position_manager=.
        bars = make_price_bars([100.0, 80.0])  # bar 2 breaches a 5% stop
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "never_signal", NeverSignalStrategy
        )
        position_manager = PositionManager()
        position_manager.register_entry(
            symbol="TEST",
            exchange="NSE",
            quantity=10,
            entry_price=100.0,
            entry_time=datetime(2025, 12, 31, 15, 20, 0),
            strategy_name="never_signal",
        )
        config = make_config("never_signal", ["TEST"], exit_policy=ExitPolicyConfig(stop_loss_pct=5.0))

        result = orchestrator.run_session(config, position_manager=position_manager)

        self.assertEqual(len(result.placed_orders), 0)
        self.assertEqual(len(result.exit_orders), 1)
        self.assertEqual(result.exit_decisions[0].reason, "stop_loss")

    def test_omitting_position_manager_behaves_exactly_as_before(self) -> None:
        # No new argument passed - every existing call site in this
        # codebase - must still start from a genuinely empty book: no
        # exit fires for a symbol nothing was ever opened for.
        bars = make_price_bars([100.0, 80.0])
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "never_signal", NeverSignalStrategy
        )
        config = make_config("never_signal", ["TEST"], exit_policy=ExitPolicyConfig(stop_loss_pct=5.0))

        result = orchestrator.run_session(config)

        self.assertEqual(len(result.exit_orders), 0)

    def test_pre_seeded_history_by_symbol_is_visible_to_the_strategy(self) -> None:
        # A caller carrying a position across a day boundary (see
        # core/backtest/multi_day_position_runner.py) also needs a
        # lookback-dependent strategy's bar HISTORY to survive the same
        # boundary - run_session's own history_by_symbol is otherwise
        # rebuilt empty every single call, silently starving any
        # strategy that needs more than the current bar's worth of
        # history (e.g. a realized-vol estimate over N prior closes)
        # even though positions/broker state correctly carried over.
        bars = make_bars(1, symbol="TEST")  # just today's one bar
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "history_reporting", HistoryReportingStrategy
        )
        history_by_symbol = {"NSE:TEST": make_bars(5, symbol="TEST")}  # 5 bars "from yesterday"
        config = make_config("history_reporting", ["TEST"])

        orchestrator.run_session(config, history_by_symbol=history_by_symbol)

        self.assertEqual(HistoryReportingStrategy.last_history_length, 5)

    def test_omitting_history_by_symbol_behaves_exactly_as_before(self) -> None:
        bars = make_bars(1, symbol="TEST")
        orchestrator, broker = make_orchestrator(
            bars, FakeRiskManager(approve=True), "history_reporting", HistoryReportingStrategy
        )
        config = make_config("history_reporting", ["TEST"])

        orchestrator.run_session(config)

        self.assertEqual(HistoryReportingStrategy.last_history_length, 0)


if __name__ == "__main__":
    unittest.main()

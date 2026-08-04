"""
Unit tests proving SignalPipeline uses a fill's actual filled_quantity,
never the requested quantity, for entries and exits alike - a live
Kite/Groww PARTIALLY_FILLED order has filled_quantity < quantity, and
using the requested amount would register/realize a phantom quantity
larger than what the broker actually holds. PaperBroker always fully
fills, so this only matters against a real broker's genuine partial
fills - these tests use a controllable fake broker instead.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_partial_fill_handling.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from core.broker.base import (
    Broker,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Position,
    ProductType,
)
from core.execution.models import TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.portfolio.exit_models import ExitPolicyConfig
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.base import RiskContext, RiskDecision, RiskManager
from core.strategy.base import MarketBar, SignalSide, Strategy, StrategyContext, TradeSignal
from core.strategy.registry import StrategyRegistry


class FakeRiskManager(RiskManager):
    def evaluate(self, order_request: OrderRequest, context: RiskContext) -> RiskDecision:
        return RiskDecision(approved=True, reason="test_approved")


class BuyOnceEverStrategy(Strategy):
    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._bought = False

    def on_bar(self, bar, history, open_positions):
        if self._bought:
            return []
        self._bought = True
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name, symbol=bar.symbol, exchange=bar.exchange,
                side=SignalSide.BUY, timestamp=bar.timestamp, reason="test_buy_once",
            )
        ]


class EntryPartiallyFillsBroker(Broker):
    """Every entry order fills for only ``filled_quantity`` of whatever
    was requested - simulates a live broker's genuine partial fill."""

    def __init__(self, filled_quantity: int) -> None:
        self._filled_quantity = filled_quantity
        self._position: Position | None = None

    def place_order(self, order_request: OrderRequest) -> Order:
        signed = self._filled_quantity if order_request.side == OrderSide.BUY else -self._filled_quantity
        self._position = Position(
            symbol=order_request.symbol, exchange=order_request.exchange, quantity=signed,
            average_price=order_request.price, product_type=order_request.product_type,
            last_price=order_request.price,
        )
        return Order(
            order_id="entry", symbol=order_request.symbol, exchange=order_request.exchange,
            side=order_request.side, quantity=order_request.quantity, filled_quantity=self._filled_quantity,
            status=OrderStatus.PARTIALLY_FILLED, order_type=order_request.order_type,
            product_type=order_request.product_type, average_price=order_request.price,
        )

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_order(self, order_id: str) -> Order:
        raise NotImplementedError

    def list_positions(self) -> list[Position]:
        return [self._position] if self._position is not None else []


class EntryFullThenExitControllableBroker(Broker):
    """The first order (the entry) always fully fills. The second order
    (the exit) gets a caller-configured status/filled_quantity - proving
    the pipeline reacts correctly to a partial or rejected exit instead
    of always assuming full success."""

    def __init__(self, exit_status: OrderStatus, exit_filled_quantity: int) -> None:
        self._exit_status = exit_status
        self._exit_filled_quantity = exit_filled_quantity
        self._orders_placed = 0
        self._position: Position | None = None

    def place_order(self, order_request: OrderRequest) -> Order:
        self._orders_placed += 1
        if self._orders_placed == 1:
            signed = order_request.quantity if order_request.side == OrderSide.BUY else -order_request.quantity
            self._position = Position(
                symbol=order_request.symbol, exchange=order_request.exchange, quantity=signed,
                average_price=order_request.price, product_type=order_request.product_type,
                last_price=order_request.price,
            )
            return Order(
                order_id="entry", symbol=order_request.symbol, exchange=order_request.exchange,
                side=order_request.side, quantity=order_request.quantity, filled_quantity=order_request.quantity,
                status=OrderStatus.FILLED, order_type=order_request.order_type,
                product_type=order_request.product_type, average_price=order_request.price,
            )

        if self._position is not None and self._exit_filled_quantity > 0:
            direction = 1 if self._position.quantity > 0 else -1
            self._position.quantity -= direction * self._exit_filled_quantity
            if self._position.quantity == 0:
                self._position = None
        return Order(
            order_id="exit", symbol=order_request.symbol, exchange=order_request.exchange,
            side=order_request.side, quantity=order_request.quantity,
            filled_quantity=self._exit_filled_quantity, status=self._exit_status,
            order_type=order_request.order_type, product_type=order_request.product_type,
            average_price=order_request.price,
        )

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_order(self, order_id: str) -> Order:
        raise NotImplementedError

    def list_positions(self) -> list[Position]:
        return [self._position] if self._position is not None else []


def make_bars(closes: list[float], symbol: str = "TEST") -> list[MarketBar]:
    base_time = datetime(2026, 1, 1, 9, 15, 0)
    return [
        MarketBar(
            symbol=symbol, exchange="NSE", timestamp=base_time + timedelta(minutes=i),
            open=close, high=close, low=close, close=close, volume=1000.0,
        )
        for i, close in enumerate(closes)
    ]


def make_plan(max_capital_per_trade: float = 1000.0) -> PortfolioAllocationPlan:
    return PortfolioAllocationPlan(
        trading_date=date(2026, 1, 1), total_capital=100000.0, reserve_capital=0.0,
        intraday_capital=100000.0, max_daily_loss=100000.0, max_trades_per_day=50,
        max_capital_per_trade=max_capital_per_trade, max_open_positions=10,
        max_symbol_allocation=100000.0, execution_mode="paper",
    )


def make_orchestrator(bars: list[MarketBar], broker: Broker) -> ExecutionOrchestrator:
    market_data_service = MarketDataService()
    market_data_service.register_feed("test_feed", MemoryMarketDataFeed(bars=bars))
    registry = StrategyRegistry()
    registry.register("buy_once", BuyOnceEverStrategy)
    return ExecutionOrchestrator(
        market_data_service=market_data_service, risk_manager=FakeRiskManager(),
        broker=broker, strategy_registry=registry,
    )


def make_config(exit_policy: ExitPolicyConfig | None = None) -> TradingSessionConfig:
    return TradingSessionConfig(
        trading_date=date(2026, 1, 1), strategy_name="buy_once", strategy_params={},
        execution_mode="paper", symbols=["TEST"], portfolio_allocation_plan=make_plan(),
        exit_policy=exit_policy,
    )


class PartialFillEntryTests(unittest.TestCase):
    def test_registered_quantity_matches_filled_not_requested(self) -> None:
        # max_capital_per_trade=1000 at price=100 -> requests qty=10;
        # broker only fills 4.
        broker = EntryPartiallyFillsBroker(filled_quantity=4)
        bars = make_bars([100.0])
        orchestrator = make_orchestrator(bars, broker)

        result = orchestrator.run_session(make_config())

        self.assertEqual(result.placed_orders[0].filled_quantity, 4)
        self.assertEqual(result.final_positions[0].quantity, 4)  # not 10


class PartialFillExitTests(unittest.TestCase):
    def test_fully_filled_exit_clears_the_position_and_realizes_full_pnl(self) -> None:
        broker = EntryFullThenExitControllableBroker(
            exit_status=OrderStatus.FILLED, exit_filled_quantity=10
        )
        bars = make_bars([100.0, 90.0])  # entry at 100, stop_loss(5%) fires at 90
        orchestrator = make_orchestrator(bars, broker)

        result = orchestrator.run_session(make_config(exit_policy=ExitPolicyConfig(stop_loss_pct=5.0)))

        self.assertEqual(result.final_positions, [])
        self.assertEqual(result.final_session_risk_state.realized_pnl, (90.0 - 100.0) * 10)

    def test_partially_filled_exit_leaves_the_remainder_tracked(self) -> None:
        # Entry fills 10; the exit only manages to close 6 of them.
        broker = EntryFullThenExitControllableBroker(
            exit_status=OrderStatus.PARTIALLY_FILLED, exit_filled_quantity=6
        )
        bars = make_bars([100.0, 90.0])
        orchestrator = make_orchestrator(bars, broker)

        result = orchestrator.run_session(make_config(exit_policy=ExitPolicyConfig(stop_loss_pct=5.0)))

        # Position still open at the broker for the unfilled remainder -
        # not silently declared flat.
        self.assertEqual(result.final_positions[0].quantity, 4)
        # PnL realized only for the 6 units that actually closed, not all 10.
        self.assertEqual(result.final_session_risk_state.realized_pnl, (90.0 - 100.0) * 6)

    def test_rejected_exit_leaves_the_position_fully_tracked_unchanged(self) -> None:
        broker = EntryFullThenExitControllableBroker(
            exit_status=OrderStatus.REJECTED, exit_filled_quantity=0
        )
        bars = make_bars([100.0, 90.0])
        orchestrator = make_orchestrator(bars, broker)

        result = orchestrator.run_session(make_config(exit_policy=ExitPolicyConfig(stop_loss_pct=5.0)))

        self.assertEqual(result.final_positions[0].quantity, 10)  # untouched
        self.assertEqual(result.final_session_risk_state.realized_pnl, 0.0)  # nothing realized


if __name__ == "__main__":
    unittest.main()

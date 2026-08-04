"""
Unit tests for the multi-leg sequencing primitive: TradeSignal.requires_prior_fill
and ExecutionOrchestrator.run_session's handling of it.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_orchestrator_multi_leg.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from core.broker.base import (
    Broker,
    OptionType,
    Order,
    OrderRequest,
    OrderStatus,
    Position,
    PositionEffect,
)
from core.broker.paper import PaperBroker
from core.execution.models import TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.base import RiskContext, RiskDecision, RiskManager
from core.strategy.base import MarketBar, SignalSide, Strategy, StrategyContext, TradeSignal
from core.strategy.registry import StrategyRegistry


class AlwaysApproveRiskManager(RiskManager):
    def evaluate(self, order_request: OrderRequest, context: RiskContext) -> RiskDecision:
        return RiskDecision(approved=True, reason="test_approved")


class ScriptedRejectionBroker(Broker):
    """
    Delegates to a real PaperBroker for everything except orders on
    ``reject_symbols`` - those are refused outright (status=REJECTED, no
    fill, no position created), simulating a real broker declining an
    order (e.g. insufficient margin). Records every place_order call
    (symbol + quantity) in ``placement_log``, in order, regardless of
    outcome - the sequencing tests below need to distinguish "attempted
    and rejected" from "never attempted at all".
    """

    def __init__(self, reject_symbols: set[str] | None = None) -> None:
        self._inner = PaperBroker()
        self._reject_symbols = reject_symbols or set()
        self.placement_log: list[tuple[str, int]] = []

    def place_order(self, order_request: OrderRequest) -> Order:
        self.placement_log.append((order_request.symbol, order_request.quantity))
        if order_request.symbol in self._reject_symbols:
            return Order(
                order_id="rejected",
                symbol=order_request.symbol,
                exchange=order_request.exchange,
                side=order_request.side,
                quantity=order_request.quantity,
                filled_quantity=0,
                status=OrderStatus.REJECTED,
                order_type=order_request.order_type,
                product_type=order_request.product_type,
            )
        return self._inner.place_order(order_request)

    def cancel_order(self, order_id: str) -> bool:
        return self._inner.cancel_order(order_id)

    def get_order(self, order_id: str) -> Order:
        return self._inner.get_order(order_id)

    def list_positions(self) -> list[Position]:
        return self._inner.list_positions()


class TwoLegSpreadStrategy(Strategy):
    """
    Emits exactly one long-then-short options signal pair, once, on the
    first bar it sees - the long leg first (requires_prior_fill=False),
    the short leg second (requires_prior_fill=True), mirroring
    VerticalCreditSpreadStrategy's real shape (see Phase 8). Deliberately
    prices the short leg's premium much higher than the long leg's
    (near-the-money short vs. further-OTM long, as in a real credit
    spread) so independent per-leg sizing would produce mismatched
    quantities if the fix didn't force them equal - the exact bug this
    primitive exists to prevent.
    """

    LONG_PRICE = 10.0
    SHORT_PRICE = 50.0

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
                symbol="LONGLEG",
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_long_leg",
                metadata={"market_price": self.LONG_PRICE},
                option_type=OptionType.CALL,
                strike=90.0,
                expiry="2026-08-27",
                lot_size=1,
                position_effect=PositionEffect.OPEN,
            ),
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol="SHORTLEG",
                exchange=bar.exchange,
                side=SignalSide.SELL,
                timestamp=bar.timestamp,
                reason="test_short_leg",
                metadata={"market_price": self.SHORT_PRICE},
                option_type=OptionType.CALL,
                strike=100.0,
                expiry="2026-08-27",
                lot_size=1,
                position_effect=PositionEffect.OPEN,
                requires_prior_fill=True,
            ),
        ]


def make_bar() -> MarketBar:
    return MarketBar(
        symbol="UNDERLYING",
        exchange="NFO",
        timestamp=datetime(2026, 1, 1, 9, 15, 0),
        open=100.0,
        high=100.0,
        low=100.0,
        close=100.0,
        volume=1000.0,
    )


def make_allocation_plan(max_capital_per_trade: float = 100.0) -> PortfolioAllocationPlan:
    return PortfolioAllocationPlan(
        trading_date=date(2026, 1, 1),
        total_capital=100000.0,
        reserve_capital=40000.0,
        intraday_capital=60000.0,
        max_daily_loss=2500.0,
        max_trades_per_day=6,
        max_capital_per_trade=max_capital_per_trade,
        max_open_positions=4,
        # Deliberately much larger than max_capital_per_trade: the short
        # leg's forced quantity (inherited from the long leg's fill, at
        # SHORT_PRICE=50.0) carries a real notional the exposure guardrail
        # must still allow through - only PositionSizingEngine's own
        # per-leg *budget* (max_capital_per_trade) is meant to be the
        # tight, illustrative constraint here, not this separate ceiling.
        max_symbol_allocation=10_000.0,
        execution_mode="paper",
    )


def make_orchestrator(broker: Broker) -> ExecutionOrchestrator:
    market_data_service = MarketDataService()
    market_data_service.register_feed("test_feed", MemoryMarketDataFeed(bars=[make_bar()]))

    registry = StrategyRegistry()
    registry.register("two_leg_spread", TwoLegSpreadStrategy)

    return ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=AlwaysApproveRiskManager(),
        broker=broker,
        strategy_registry=registry,
    )


def make_config(max_capital_per_trade: float = 100.0) -> TradingSessionConfig:
    return TradingSessionConfig(
        trading_date=date(2026, 1, 1),
        strategy_name="two_leg_spread",
        strategy_params={},
        execution_mode="paper",
        symbols=["UNDERLYING"],
        portfolio_allocation_plan=make_allocation_plan(max_capital_per_trade),
    )


class BothLegsFillTests(unittest.TestCase):
    def test_short_leg_inherits_long_legs_quantity_not_its_own_sizing(self) -> None:
        # Long (price=10) affords floor(100/10)=10 at capital=100; short
        # (price=50) would independently only afford floor(100/50)=2 -
        # the short must instead be forced to 10, matching the long.
        broker = ScriptedRejectionBroker()
        orchestrator = make_orchestrator(broker)

        result = orchestrator.run_session(make_config(max_capital_per_trade=100.0))

        self.assertEqual(len(result.placed_orders), 2)
        long_order = next(o for o in result.placed_orders if o.symbol == "LONGLEG")
        short_order = next(o for o in result.placed_orders if o.symbol == "SHORTLEG")
        self.assertEqual(long_order.quantity, 10)
        self.assertEqual(short_order.quantity, 10)  # inherited, not floor(100/50)=2
        self.assertEqual(result.approved_signals, 2)
        self.assertEqual(result.rejected_signals, 0)

    def test_both_legs_appear_in_final_positions(self) -> None:
        broker = ScriptedRejectionBroker()
        orchestrator = make_orchestrator(broker)

        result = orchestrator.run_session(make_config(max_capital_per_trade=100.0))

        symbols_with_positions = {p.symbol for p in result.final_positions if p.quantity != 0}
        self.assertEqual(symbols_with_positions, {"LONGLEG", "SHORTLEG"})


class LongLegRejectedTests(unittest.TestCase):
    def test_short_leg_is_never_attempted_when_long_leg_is_rejected(self) -> None:
        broker = ScriptedRejectionBroker(reject_symbols={"LONGLEG"})
        orchestrator = make_orchestrator(broker)

        result = orchestrator.run_session(make_config(max_capital_per_trade=100.0))

        # place_order was called for LONGLEG (and rejected) but never for
        # SHORTLEG at all - not attempted-and-rejected, genuinely skipped.
        attempted_symbols = [symbol for symbol, _ in broker.placement_log]
        self.assertEqual(attempted_symbols, ["LONGLEG"])
        # LONGLEG was attempted and rejected (still appears in placed_orders
        # with REJECTED status - result.placed_orders records every order
        # sent to the broker regardless of outcome); SHORTLEG never got
        # this far at all, so it's absent, not merely rejected.
        self.assertEqual(len(result.placed_orders), 1)
        self.assertEqual(result.placed_orders[0].symbol, "LONGLEG")
        self.assertEqual(result.placed_orders[0].status, OrderStatus.REJECTED)
        self.assertEqual(result.final_positions, [])

    def test_short_leg_rejection_reason_is_prior_leg_not_filled(self) -> None:
        class RecordingAuditSink:
            def __init__(self) -> None:
                self.events = []

            def record_event(self, event) -> None:
                self.events.append(event)

        broker = ScriptedRejectionBroker(reject_symbols={"LONGLEG"})
        orchestrator = make_orchestrator(broker)
        sink = RecordingAuditSink()
        orchestrator.audit_sink = sink

        orchestrator.run_session(make_config(max_capital_per_trade=100.0))

        rejection_reasons = [
            e.payload.get("rejection_stage")
            for e in sink.events
            if e.payload.get("rejection_stage") is not None
        ]
        self.assertIn("prior_leg_not_filled", rejection_reasons)


class ShortLegRejectedTests(unittest.TestCase):
    def test_long_position_stands_when_short_leg_is_rejected(self) -> None:
        # The safe-fallback outcome: a rejected hedge leg must not roll
        # back the already-filled protective leg - the result is a
        # standalone long option (bounded, safe loss), never a naked
        # short and never nothing.
        broker = ScriptedRejectionBroker(reject_symbols={"SHORTLEG"})
        orchestrator = make_orchestrator(broker)

        result = orchestrator.run_session(make_config(max_capital_per_trade=100.0))

        attempted_symbols = [symbol for symbol, _ in broker.placement_log]
        self.assertEqual(attempted_symbols, ["LONGLEG", "SHORTLEG"])  # short WAS attempted

        # Both appear in placed_orders (every attempt is recorded there
        # regardless of outcome) - LONGLEG filled, SHORTLEG rejected.
        self.assertEqual(len(result.placed_orders), 2)
        long_order = next(o for o in result.placed_orders if o.symbol == "LONGLEG")
        short_order = next(o for o in result.placed_orders if o.symbol == "SHORTLEG")
        self.assertEqual(long_order.status, OrderStatus.FILLED)
        self.assertEqual(short_order.status, OrderStatus.REJECTED)

        symbols_with_positions = {p.symbol for p in result.final_positions if p.quantity != 0}
        self.assertEqual(symbols_with_positions, {"LONGLEG"})

    def test_short_leg_was_sized_at_the_long_legs_quantity_before_rejection(self) -> None:
        broker = ScriptedRejectionBroker(reject_symbols={"SHORTLEG"})
        orchestrator = make_orchestrator(broker)

        orchestrator.run_session(make_config(max_capital_per_trade=100.0))

        short_leg_attempt = next(
            (symbol, qty) for symbol, qty in broker.placement_log if symbol == "SHORTLEG"
        )
        self.assertEqual(short_leg_attempt, ("SHORTLEG", 10))  # inherited qty, not its own sizing


if __name__ == "__main__":
    unittest.main()

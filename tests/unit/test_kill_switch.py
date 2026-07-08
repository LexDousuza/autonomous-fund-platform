"""
Unit tests for core.execution.kill_switch.flatten_all.

Covers: flattening long and short positions, zero-quantity positions
being skipped, cancelling known open orders (confirmed / not confirmed /
erroring), best-effort continuation past individual failures, the
positions-fetch-failure hard-stop, and audit event emission.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_kill_switch.py -v
"""
from __future__ import annotations

import unittest

from core.audit.models import AuditEvent, AuditEventType
from core.audit.sink import AuditSink
from core.broker.base import (
    Broker,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ProductType,
)
from core.broker.paper import PaperBroker
from core.execution.kill_switch import flatten_all


class InMemoryAuditSink(AuditSink):
    """Test double: records everything in memory, no file I/O."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def start_session(self, metadata) -> None:  # noqa: ANN001 - unused in these tests
        pass

    def record_event(self, event: AuditEvent) -> None:
        self.events.append(event)

    def finalize_session(self, summary) -> None:  # noqa: ANN001 - unused in these tests
        pass

    def record_failure(self, error: BaseException) -> None:
        pass

    def events_of_type(self, event_type: AuditEventType) -> list[AuditEvent]:
        return [e for e in self.events if e.event_type == event_type]


class FakeBroker(Broker):
    """
    Controllable ``Broker`` test double: canned positions, and per-symbol
    / per-order-id failure injection for ``place_order``/``cancel_order``,
    so best-effort continuation past individual failures can be verified
    deterministically (something ``PaperBroker`` alone can't do).
    """

    def __init__(self, positions: list[Position]) -> None:
        self._positions = positions
        self.placed_orders: list[OrderRequest] = []
        self.cancelled_order_ids: list[str] = []
        self.fail_place_for_symbols: set[str] = set()
        self.fail_cancel_for_order_ids: dict[str, Exception] = {}
        self.cancel_confirms: dict[str, bool] = {}
        self.list_positions_error: Exception | None = None

    def place_order(self, order_request: OrderRequest) -> Order:
        self.placed_orders.append(order_request)
        if order_request.symbol in self.fail_place_for_symbols:
            raise RuntimeError(f"broker rejected flatten order for {order_request.symbol}")
        return Order(
            order_id=f"flatten-{order_request.symbol}",
            symbol=order_request.symbol,
            exchange=order_request.exchange,
            side=order_request.side,
            quantity=order_request.quantity,
            filled_quantity=order_request.quantity,
            status=OrderStatus.FILLED,
            order_type=order_request.order_type,
            product_type=order_request.product_type,
            average_price=order_request.price,
        )

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled_order_ids.append(order_id)
        if order_id in self.fail_cancel_for_order_ids:
            raise self.fail_cancel_for_order_ids[order_id]
        return self.cancel_confirms.get(order_id, True)

    def get_order(self, order_id: str) -> Order:
        raise NotImplementedError

    def list_positions(self) -> list[Position]:
        if self.list_positions_error is not None:
            raise self.list_positions_error
        return self._positions


def _position(symbol: str, quantity: int, exchange: str = "NSE") -> Position:
    return Position(
        symbol=symbol,
        exchange=exchange,
        quantity=quantity,
        average_price=100.0,
        product_type=ProductType.INTRADAY,
        last_price=105.0,
    )


class FlattenAllPositionsTests(unittest.TestCase):
    def test_no_open_positions_is_a_clean_noop(self) -> None:
        broker = FakeBroker(positions=[])

        result = flatten_all(broker)

        self.assertEqual(result.flattened_positions, [])
        self.assertTrue(result.succeeded)
        self.assertEqual(broker.placed_orders, [])

    def test_zero_quantity_positions_are_skipped(self) -> None:
        broker = FakeBroker(positions=[_position("INFY", 0)])

        result = flatten_all(broker)

        self.assertEqual(result.flattened_positions, [])
        self.assertEqual(broker.placed_orders, [])

    def test_long_position_is_flattened_with_a_sell_market_order(self) -> None:
        broker = FakeBroker(positions=[_position("RELIANCE", 50)])

        result = flatten_all(broker)

        self.assertEqual(len(broker.placed_orders), 1)
        request = broker.placed_orders[0]
        self.assertEqual(request.side, OrderSide.SELL)
        self.assertEqual(request.quantity, 50)
        self.assertEqual(request.order_type, OrderType.MARKET)
        self.assertEqual(request.product_type, ProductType.INTRADAY)
        self.assertEqual(len(result.flattened_positions), 1)
        self.assertTrue(result.succeeded)

    def test_short_position_is_flattened_with_a_buy_market_order(self) -> None:
        broker = FakeBroker(positions=[_position("TCS", -30)])

        result = flatten_all(broker)

        request = broker.placed_orders[0]
        self.assertEqual(request.side, OrderSide.BUY)
        self.assertEqual(request.quantity, 30)
        self.assertTrue(result.succeeded)

    def test_one_failed_flatten_does_not_block_the_rest(self) -> None:
        broker = FakeBroker(
            positions=[_position("RELIANCE", 50), _position("TCS", 20), _position("INFY", -10)]
        )
        broker.fail_place_for_symbols = {"TCS"}

        result = flatten_all(broker)

        flattened_symbols = {order.symbol for order in result.flattened_positions}
        self.assertEqual(flattened_symbols, {"RELIANCE", "INFY"})
        self.assertEqual(len(result.failed_flattens), 1)
        self.assertEqual(result.failed_flattens[0].symbol, "TCS")
        self.assertFalse(result.succeeded)
        # All three positions were still attempted, not just up to the failure.
        self.assertEqual(len(broker.placed_orders), 3)

    def test_positions_fetch_failure_propagates_and_is_recorded(self) -> None:
        broker = FakeBroker(positions=[])
        broker.list_positions_error = RuntimeError("broker API is down")
        sink = InMemoryAuditSink()

        with self.assertRaises(RuntimeError):
            flatten_all(broker, audit_sink=sink)

        fetch_failed = sink.events_of_type(AuditEventType.KILL_SWITCH_POSITIONS_FETCH_FAILED)
        self.assertEqual(len(fetch_failed), 1)

    def test_real_paper_broker_end_to_end(self) -> None:
        """Places real orders against PaperBroker to build up positions, then flattens them."""
        broker = PaperBroker()
        broker.place_order(
            OrderRequest(
                symbol="RELIANCE",
                exchange="NSE",
                side=OrderSide.BUY,
                quantity=10,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                price=2500.0,
            )
        )
        broker.place_order(
            OrderRequest(
                symbol="TCS",
                exchange="NSE",
                side=OrderSide.SELL,
                quantity=5,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                price=3800.0,
            )
        )

        result = flatten_all(broker)

        self.assertTrue(result.succeeded)
        self.assertEqual({p.symbol for p in result.flattened_positions}, {"RELIANCE", "TCS"})
        final_positions = {p.symbol: p.quantity for p in broker.list_positions()}
        self.assertEqual(final_positions["RELIANCE"], 0)
        self.assertEqual(final_positions["TCS"], 0)


class FlattenAllOrderCancellationTests(unittest.TestCase):
    def test_confirmed_cancellation_is_recorded(self) -> None:
        broker = FakeBroker(positions=[])
        broker.cancel_confirms["order-1"] = True

        result = flatten_all(broker, open_order_ids=["order-1"])

        self.assertEqual(result.cancelled_order_ids, ["order-1"])
        self.assertTrue(result.succeeded)

    def test_not_confirmed_cancellation_counts_as_a_failure(self) -> None:
        broker = FakeBroker(positions=[])
        broker.cancel_confirms["order-1"] = False

        result = flatten_all(broker, open_order_ids=["order-1"])

        self.assertEqual(result.not_confirmed_cancellations, ["order-1"])
        self.assertFalse(result.succeeded)

    def test_cancel_error_does_not_block_remaining_orders_or_positions(self) -> None:
        broker = FakeBroker(positions=[_position("RELIANCE", 10)])
        broker.fail_cancel_for_order_ids["order-bad"] = RuntimeError("network error")

        result = flatten_all(broker, open_order_ids=["order-bad", "order-good"])

        self.assertEqual(len(result.failed_cancellations), 1)
        self.assertEqual(result.failed_cancellations[0].order_id, "order-bad")
        self.assertEqual(result.cancelled_order_ids, ["order-good"])
        # Position flattening still ran after the cancellation failure.
        self.assertEqual(len(result.flattened_positions), 1)
        self.assertFalse(result.succeeded)


class FlattenAllAuditEventTests(unittest.TestCase):
    def test_full_lifecycle_emits_expected_event_sequence(self) -> None:
        broker = FakeBroker(positions=[_position("RELIANCE", 10)])
        sink = InMemoryAuditSink()

        flatten_all(broker, open_order_ids=["order-1"], audit_sink=sink, reason="test_reason")

        event_types = [e.event_type for e in sink.events]
        self.assertEqual(
            event_types,
            [
                AuditEventType.KILL_SWITCH_TRIGGERED,
                AuditEventType.KILL_SWITCH_ORDER_CANCELLED,
                AuditEventType.KILL_SWITCH_POSITION_FLATTENED,
                AuditEventType.KILL_SWITCH_COMPLETED,
            ],
        )
        self.assertEqual(sink.events[0].payload["reason"], "test_reason")

    def test_no_audit_sink_is_a_safe_noop(self) -> None:
        broker = FakeBroker(positions=[_position("RELIANCE", 10)])

        result = flatten_all(broker)  # must not raise despite audit_sink=None

        self.assertTrue(result.succeeded)


if __name__ == "__main__":
    unittest.main()

"""
Unit tests for core.broker.kite_margin.KiteMarginCalculator.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_kite_margin.py -v
"""
from __future__ import annotations

import unittest

from core.broker.base import OptionType, OrderRequest, OrderSide, OrderType, PositionEffect, ProductType
from core.broker.kite_errors import KiteTransportError
from core.broker.kite_margin import KiteMarginCalculator, fetch_available_margin


class FakeKiteTransport:
    def __init__(self) -> None:
        self.get_order_margins_calls: list[list[dict]] = []
        self.response: list[dict] = [{"total": 12500.0}]
        self.get_margins_calls: list[str | None] = []
        self.margins_response: dict = {"net": 50000.0, "available": {}, "utilised": {}}

    def get_order_margins(self, orders: list[dict]) -> list[dict]:
        self.get_order_margins_calls.append(orders)
        return self.response

    def get_margins(self, segment: str | None = None) -> dict:
        self.get_margins_calls.append(segment)
        return self.margins_response


def make_naked_write_order() -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY26AUG23000CE",
        exchange="NFO",
        side=OrderSide.SELL,
        quantity=50,
        order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY,
        price=120.0,
        option_type=OptionType.CALL,
        strike=23000.0,
        expiry="2026-08-27",
        lot_size=50,
        position_effect=PositionEffect.OPEN,
    )


class KiteMarginCalculatorTests(unittest.TestCase):
    def test_required_margin_reads_total_field(self) -> None:
        transport = FakeKiteTransport()
        calculator = KiteMarginCalculator(transport)

        result = calculator.required_margin(make_naked_write_order())

        self.assertEqual(result, 12500.0)

    def test_sends_single_order_with_expected_fields(self) -> None:
        transport = FakeKiteTransport()
        calculator = KiteMarginCalculator(transport)

        calculator.required_margin(make_naked_write_order())

        orders = transport.get_order_margins_calls[0]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["tradingsymbol"], "NIFTY26AUG23000CE")
        self.assertEqual(orders[0]["transaction_type"], "SELL")
        self.assertEqual(orders[0]["exchange"], "NFO")
        self.assertEqual(orders[0]["product"], "MIS")
        self.assertEqual(orders[0]["variety"], "regular")
        self.assertEqual(orders[0]["quantity"], 50)

    def test_empty_response_raises_transport_error(self) -> None:
        transport = FakeKiteTransport()
        transport.response = []
        calculator = KiteMarginCalculator(transport)

        with self.assertRaises(KiteTransportError):
            calculator.required_margin(make_naked_write_order())

    def test_missing_field_raises_transport_error(self) -> None:
        transport = FakeKiteTransport()
        transport.response = [{}]  # missing "total"
        calculator = KiteMarginCalculator(transport)

        with self.assertRaises(KiteTransportError):
            calculator.required_margin(make_naked_write_order())


class FetchAvailableMarginTests(unittest.TestCase):
    def test_reads_net_field_from_segment_response(self) -> None:
        transport = FakeKiteTransport()

        result = fetch_available_margin(transport)

        self.assertEqual(result, 50000.0)

    def test_defaults_to_equity_segment(self) -> None:
        transport = FakeKiteTransport()

        fetch_available_margin(transport)

        self.assertEqual(transport.get_margins_calls, ["equity"])

    def test_honors_explicit_segment(self) -> None:
        transport = FakeKiteTransport()

        fetch_available_margin(transport, segment="commodity")

        self.assertEqual(transport.get_margins_calls, ["commodity"])

    def test_missing_net_field_raises_transport_error(self) -> None:
        transport = FakeKiteTransport()
        transport.margins_response = {"available": {}, "utilised": {}}  # missing "net"

        with self.assertRaises(KiteTransportError):
            fetch_available_margin(transport)


if __name__ == "__main__":
    unittest.main()

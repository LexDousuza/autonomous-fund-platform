"""
Unit tests for core.broker.groww_margin.GrowwMarginCalculator.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_groww_margin.py -v
"""
from __future__ import annotations

import unittest

from core.broker.base import OptionType, OrderRequest, OrderSide, OrderType, PositionEffect, ProductType
from core.broker.groww_errors import GrowwTransportError
from core.broker.groww_margin import GrowwMarginCalculator


class FakeGrowwTransport:
    def __init__(self) -> None:
        self.get_order_margin_details_calls: list[tuple[list[dict], str]] = []
        self.response: dict = {"total_requirement": 12500.0}

    def get_order_margin_details(self, orders: list[dict], segment: str) -> dict:
        self.get_order_margin_details_calls.append((orders, segment))
        return self.response


def make_naked_write_order() -> OrderRequest:
    return OrderRequest(
        symbol="RELIANCE26AUG2600CE",
        exchange="NFO",
        side=OrderSide.SELL,
        quantity=250,
        order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY,
        price=15.0,
        option_type=OptionType.CALL,
        strike=2600.0,
        expiry="2026-08-26",
        lot_size=250,
        position_effect=PositionEffect.OPEN,
    )


class GrowwMarginCalculatorTests(unittest.TestCase):
    def test_required_margin_reads_total_requirement(self) -> None:
        transport = FakeGrowwTransport()
        calculator = GrowwMarginCalculator(transport)

        result = calculator.required_margin(make_naked_write_order())

        self.assertEqual(result, 12500.0)

    def test_sends_single_order_batch_to_fno_segment(self) -> None:
        transport = FakeGrowwTransport()
        calculator = GrowwMarginCalculator(transport)

        calculator.required_margin(make_naked_write_order())

        orders, segment = transport.get_order_margin_details_calls[0]
        self.assertEqual(len(orders), 1)
        self.assertEqual(segment, "FNO")
        self.assertEqual(orders[0]["trading_symbol"], "RELIANCE26AUG2600CE")
        self.assertEqual(orders[0]["transaction_type"], "SELL")
        self.assertEqual(orders[0]["exchange"], "NFO")
        self.assertEqual(orders[0]["quantity"], 250)

    def test_missing_field_raises_transport_error(self) -> None:
        transport = FakeGrowwTransport()
        transport.response = {}  # missing total_requirement
        calculator = GrowwMarginCalculator(transport)

        with self.assertRaises(GrowwTransportError):
            calculator.required_margin(make_naked_write_order())


if __name__ == "__main__":
    unittest.main()

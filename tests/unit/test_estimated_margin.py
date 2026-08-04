"""
Unit tests for core.broker.estimated_margin.EstimatedSpanMarginCalculator.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_estimated_margin.py -v
"""
from __future__ import annotations

import unittest

from core.broker.base import OptionType, OrderRequest, OrderSide, OrderType, PositionEffect, ProductType
from core.broker.estimated_margin import EstimatedSpanMarginCalculator


def make_naked_write_order(strike: float = 23000.0, quantity: int = 50) -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY26AUG23000CE",
        exchange="NFO",
        side=OrderSide.SELL,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY,
        price=120.0,
        option_type=OptionType.CALL,
        strike=strike,
        expiry="2026-08-27",
        lot_size=quantity,
        position_effect=PositionEffect.OPEN,
    )


class EstimatedSpanMarginCalculatorTests(unittest.TestCase):
    def test_estimates_as_fraction_of_strike_times_quantity(self) -> None:
        calculator = EstimatedSpanMarginCalculator(margin_fraction=0.20)

        result = calculator.required_margin(make_naked_write_order(strike=23000.0, quantity=50))

        self.assertEqual(result, 23000.0 * 50 * 0.20)

    def test_uses_premium_never_as_the_notional_base(self) -> None:
        # price=120.0 (the premium) must never leak into the estimate -
        # only strike (the notional proxy) should. Regression guard for
        # the "premium understates real margin" trap this class exists
        # to avoid.
        order = make_naked_write_order(strike=23000.0, quantity=50)
        calculator = EstimatedSpanMarginCalculator(margin_fraction=0.20)

        result = calculator.required_margin(order)

        self.assertGreater(result, order.price * order.quantity)

    def test_custom_margin_fraction_is_honored(self) -> None:
        calculator = EstimatedSpanMarginCalculator(margin_fraction=0.5)

        result = calculator.required_margin(make_naked_write_order(strike=1000.0, quantity=10))

        self.assertEqual(result, 1000.0 * 10 * 0.5)

    def test_missing_strike_estimates_zero_rather_than_raising(self) -> None:
        order = make_naked_write_order()
        order.strike = None
        calculator = EstimatedSpanMarginCalculator()

        result = calculator.required_margin(order)

        self.assertEqual(result, 0.0)


if __name__ == "__main__":
    unittest.main()

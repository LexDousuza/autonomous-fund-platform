"""
Unit tests for core.portfolio.sizing.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_position_sizing.py -v
"""
from __future__ import annotations

import unittest
from datetime import date

from core.portfolio.models import PortfolioAllocationPlan
from core.portfolio.sizing import PositionSizingEngine
from core.portfolio.sizing_models import PositionSizingRequest
from core.strategy.base import SignalSide


def make_allocation_plan(max_capital_per_trade: float = 15000.0) -> PortfolioAllocationPlan:
    return PortfolioAllocationPlan(
        trading_date=date(2026, 1, 1),
        total_capital=100000.0,
        reserve_capital=40000.0,
        intraday_capital=60000.0,
        max_daily_loss=2500.0,
        max_trades_per_day=6,
        max_capital_per_trade=max_capital_per_trade,
        max_open_positions=4,
        max_symbol_allocation=max_capital_per_trade,
        execution_mode="paper",
    )


def make_request(
    price: float, max_capital_per_trade: float = 15000.0, lot_size: int = 1
) -> PositionSizingRequest:
    return PositionSizingRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=SignalSide.BUY,
        price=price,
        portfolio_allocation_plan=make_allocation_plan(max_capital_per_trade),
        lot_size=lot_size,
    )


class PositionSizingEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PositionSizingEngine()

    def test_normal_sizing_floors_to_whole_shares(self) -> None:
        # 15000 / 2500 = 6.0 exactly
        request = make_request(price=2500.0, max_capital_per_trade=15000.0)

        decision = self.engine.size(request)

        self.assertEqual(decision.approved_quantity, 6)
        self.assertEqual(decision.max_affordable_quantity, 6)
        self.assertEqual(decision.capital_used, 15000.0)
        self.assertEqual(decision.reason, "approved")

    def test_normal_sizing_floors_down_on_remainder(self) -> None:
        # 15000 / 2600 = 5.77 -> floor to 5
        request = make_request(price=2600.0, max_capital_per_trade=15000.0)

        decision = self.engine.size(request)

        self.assertEqual(decision.approved_quantity, 5)
        self.assertEqual(decision.max_affordable_quantity, 5)
        self.assertEqual(decision.capital_used, 13000.0)
        self.assertEqual(decision.reason, "approved")

    def test_zero_price_yields_zero_quantity(self) -> None:
        request = make_request(price=0.0)

        decision = self.engine.size(request)

        self.assertEqual(decision.approved_quantity, 0)
        self.assertEqual(decision.capital_used, 0.0)
        self.assertEqual(decision.max_affordable_quantity, 0)
        self.assertEqual(decision.reason, "invalid_price")

    def test_negative_price_yields_zero_quantity(self) -> None:
        request = make_request(price=-100.0)

        decision = self.engine.size(request)

        self.assertEqual(decision.approved_quantity, 0)
        self.assertEqual(decision.reason, "invalid_price")

    def test_zero_capital_per_trade_yields_zero_quantity(self) -> None:
        request = make_request(price=100.0, max_capital_per_trade=0.0)

        decision = self.engine.size(request)

        self.assertEqual(decision.approved_quantity, 0)
        self.assertEqual(decision.capital_used, 0.0)
        self.assertEqual(decision.reason, "no_capital_allocated_per_trade")

    def test_negative_capital_per_trade_yields_zero_quantity(self) -> None:
        request = make_request(price=100.0, max_capital_per_trade=-500.0)

        decision = self.engine.size(request)

        self.assertEqual(decision.approved_quantity, 0)
        self.assertEqual(decision.reason, "no_capital_allocated_per_trade")

    def test_price_too_high_to_afford_one_share(self) -> None:
        request = make_request(price=50000.0, max_capital_per_trade=15000.0)

        decision = self.engine.size(request)

        self.assertEqual(decision.approved_quantity, 0)
        self.assertEqual(decision.max_affordable_quantity, 0)
        self.assertEqual(decision.capital_used, 0.0)
        self.assertEqual(decision.reason, "price_exceeds_capital_per_trade")

    def test_decision_is_deterministic(self) -> None:
        request = make_request(price=2492.0, max_capital_per_trade=15000.0)

        first = self.engine.size(request)
        second = self.engine.size(request)

        self.assertEqual(first, second)

    def test_lot_size_one_is_byte_identical_to_default(self) -> None:
        # lot_size=1 (the default) must reproduce today's plain floor(budget/price).
        request = make_request(price=2600.0, max_capital_per_trade=15000.0, lot_size=1)

        decision = self.engine.size(request)

        self.assertEqual(decision.approved_quantity, 5)
        self.assertEqual(decision.capital_used, 13000.0)

    def test_lot_size_rounds_down_to_whole_lots(self) -> None:
        # budget 15000, price 100/unit, lot_size 50 -> 15000/(100*50)=3.0 lots -> 150 units
        request = make_request(price=100.0, max_capital_per_trade=15000.0, lot_size=50)

        decision = self.engine.size(request)

        self.assertEqual(decision.approved_quantity, 150)
        self.assertEqual(decision.max_affordable_quantity, 150)
        self.assertEqual(decision.capital_used, 15000.0)
        self.assertEqual(decision.reason, "approved")

    def test_lot_size_rounds_down_on_remainder(self) -> None:
        # budget 15000, price 100/unit, lot_size 40 -> 15000/(100*40)=3.75 lots -> floor to 3 -> 120 units
        request = make_request(price=100.0, max_capital_per_trade=15000.0, lot_size=40)

        decision = self.engine.size(request)

        self.assertEqual(decision.approved_quantity, 120)
        self.assertEqual(decision.capital_used, 12000.0)

    def test_lot_size_too_large_for_one_lot_is_rejected(self) -> None:
        # one lot alone (100 * 500 = 50000) exceeds the 15000 budget
        request = make_request(price=100.0, max_capital_per_trade=15000.0, lot_size=500)

        decision = self.engine.size(request)

        self.assertEqual(decision.approved_quantity, 0)
        self.assertEqual(decision.reason, "price_exceeds_capital_per_trade")


if __name__ == "__main__":
    unittest.main()

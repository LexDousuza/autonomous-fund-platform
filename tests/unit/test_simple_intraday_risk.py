"""Unit tests for core.risk.simple_intraday and core.broker.groww_fees."""
from __future__ import annotations

import unittest

from core.broker.base import OrderRequest, OrderSide, OrderType, ProductType
from core.broker.groww_fees import estimate_groww_brokerage
from core.risk.base import RiskContext
from core.risk.simple_intraday import SimpleIntradayRiskManager


def make_order(price: float = 100.0, quantity: int = 100) -> OrderRequest:
    return OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY,
        price=price,
        strategy_name="test_strategy",
    )


def make_context(target_pct: float | None) -> RiskContext:
    return RiskContext(
        available_intraday_capital=60000.0,
        used_intraday_capital=0.0,
        daily_realized_pnl=0.0,
        trades_taken_today=0,
        max_daily_loss=2500.0,
        max_trades_per_day=6,
        target_pct=target_pct,
    )


class GrowwBrokerageTests(unittest.TestCase):
    def test_floors_at_minimum(self) -> None:
        self.assertEqual(estimate_groww_brokerage(2000.0), 5.0)

    def test_caps_at_maximum(self) -> None:
        self.assertEqual(estimate_groww_brokerage(25000.0), 20.0)

    def test_uses_rate_between_bounds(self) -> None:
        self.assertAlmostEqual(estimate_groww_brokerage(10000.0), 10.0)


class ProfitVsBrokerageGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.risk_manager = SimpleIntradayRiskManager()
        # price=100, quantity=100 -> notional=10000 -> brokerage=10, min_required_profit=20
        self.order = make_order(price=100.0, quantity=100)

    def test_skipped_when_no_target_pct(self) -> None:
        decision = self.risk_manager.evaluate(self.order, make_context(target_pct=None))
        self.assertTrue(decision.approved)

    def test_rejects_when_projected_profit_below_threshold(self) -> None:
        # target_pct=0.1% -> projected profit = 10 <= min_required_profit (20)
        decision = self.risk_manager.evaluate(self.order, make_context(target_pct=0.1))
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "projected_profit_below_brokerage_threshold")

    def test_approves_when_projected_profit_clears_threshold(self) -> None:
        # target_pct=2% -> projected profit = 200 > min_required_profit (20)
        decision = self.risk_manager.evaluate(self.order, make_context(target_pct=2.0))
        self.assertTrue(decision.approved)


if __name__ == "__main__":
    unittest.main()

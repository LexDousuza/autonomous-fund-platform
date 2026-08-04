"""Unit tests for core.risk.simple_intraday and core.broker.groww_fees."""
from __future__ import annotations

import unittest

from core.broker.base import OrderRequest, OrderSide, OrderType, ProductType
from core.broker.groww_fees import estimate_groww_brokerage
from core.risk.base import RiskContext
from core.risk.simple_intraday import (
    DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES,
    IntradayRiskSettings,
    SimpleIntradayRiskManager,
)


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


def make_context(
    target_pct: float | None, strategy_name: str | None = None
) -> RiskContext:
    return RiskContext(
        available_intraday_capital=60000.0,
        used_intraday_capital=0.0,
        daily_realized_pnl=0.0,
        trades_taken_today=0,
        max_daily_loss=2500.0,
        max_trades_per_day=6,
        target_pct=target_pct,
        strategy_name=strategy_name,
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


class PerStrategyBrokerageRatioOverrideTests(unittest.TestCase):
    """
    OU's real numbers: notional=2000 (price=20, quantity=100), target_pct=0.4%
    -> projected_profit = 8.0, brokerage = estimate_groww_brokerage(2000) = 5.0
    (Groww's Rs 5 floor). At the global ratio (2.0) min_required=10 -> rejected.
    At the ornstein_uhlenbeck override (1.3) min_required=6.5 -> approved.
    """

    def setUp(self) -> None:
        self.risk_manager = SimpleIntradayRiskManager(
            IntradayRiskSettings(
                min_profit_to_brokerage_ratio_by_strategy=(
                    DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES
                )
            )
        )
        self.order = make_order(price=20.0, quantity=100)

    def test_override_approves_ou_at_its_real_numbers(self) -> None:
        decision = self.risk_manager.evaluate(
            self.order,
            make_context(target_pct=0.4, strategy_name="ornstein_uhlenbeck"),
        )
        self.assertTrue(decision.approved)

    def test_unlisted_strategy_still_rejects_at_same_numbers(self) -> None:
        # Same order/target_pct, but strategy_name=None -> falls through to
        # the global 2.0 ratio, min_required=10 > profit(8) -> still rejected.
        decision = self.risk_manager.evaluate(
            self.order, make_context(target_pct=0.4, strategy_name=None)
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "projected_profit_below_brokerage_threshold")

    def test_unknown_strategy_name_also_still_rejects(self) -> None:
        decision = self.risk_manager.evaluate(
            self.order,
            make_context(target_pct=0.4, strategy_name="some_other_strategy"),
        )
        self.assertFalse(decision.approved)


class RiskContextStrategyNameDefaultTests(unittest.TestCase):
    def test_strategy_name_defaults_to_none(self) -> None:
        context = make_context(target_pct=None)
        self.assertIsNone(context.strategy_name)


if __name__ == "__main__":
    unittest.main()

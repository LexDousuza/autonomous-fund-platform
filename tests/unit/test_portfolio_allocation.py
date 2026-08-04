"""
Unit tests for core.portfolio.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_portfolio_allocation.py -v
"""
from __future__ import annotations

import unittest
from datetime import date

from core.allocations.daily_mandate import (
    DailyCapitalBuckets,
    DailyMandate,
    DailyRiskLimits,
    load_daily_mandate,
)
from core.portfolio.allocation import (
    PortfolioAllocationBuilder,
    PortfolioAllocationSettings,
    build_portfolio_allocation_plan,
    build_portfolio_allocation_plans,
)

MANDATE_PATH = "config/mandates/sample_mandate.yaml"


def make_mandate(
    reserve_bucket: float = 40000.0,
    intraday_bucket: float = 60000.0,
    max_daily_loss: float = 2500.0,
    max_trades_per_day: int = 6,
    execution_mode: str = "paper",
) -> DailyMandate:
    return DailyMandate(
        trading_date=date(2026, 7, 7),
        buckets=DailyCapitalBuckets(
            reserve_bucket=reserve_bucket,
            intraday_bucket=intraday_bucket,
        ),
        risk_limits=DailyRiskLimits(
            max_daily_loss=max_daily_loss,
            max_trades_per_day=max_trades_per_day,
        ),
        execution_mode=execution_mode,
    )


class PortfolioAllocationBuilderTests(unittest.TestCase):
    def test_v1_default_rules(self) -> None:
        mandate = make_mandate()
        plan = build_portfolio_allocation_plan(mandate)

        self.assertEqual(plan.trading_date, mandate.trading_date)
        self.assertEqual(plan.total_capital, 100000.0)
        self.assertEqual(plan.reserve_capital, 40000.0)
        self.assertEqual(plan.intraday_capital, 60000.0)
        self.assertEqual(plan.max_daily_loss, 2500.0)
        self.assertEqual(plan.max_trades_per_day, 6)
        self.assertEqual(plan.max_capital_per_trade, 15000.0)  # 60000 / 4
        self.assertEqual(plan.max_open_positions, 4)
        self.assertEqual(plan.max_symbol_allocation, plan.max_capital_per_trade)
        self.assertEqual(plan.execution_mode, "paper")

    def test_deterministic_for_same_mandate(self) -> None:
        mandate = make_mandate()
        plan_a = build_portfolio_allocation_plan(mandate)
        plan_b = build_portfolio_allocation_plan(mandate)

        self.assertEqual(plan_a, plan_b)

    def test_custom_settings_override_divisor_and_open_positions(self) -> None:
        mandate = make_mandate(intraday_bucket=100000.0)
        settings = PortfolioAllocationSettings(
            max_capital_per_trade_divisor=5.0,
            default_max_open_positions=2,
        )
        plan = PortfolioAllocationBuilder(settings).build(mandate)

        self.assertEqual(plan.max_capital_per_trade, 20000.0)  # 100000 / 5
        self.assertEqual(plan.max_open_positions, 2)
        self.assertEqual(plan.max_symbol_allocation, 20000.0)

    def test_zero_intraday_bucket_yields_zero_per_trade_cap(self) -> None:
        mandate = make_mandate(intraday_bucket=0.0)
        plan = build_portfolio_allocation_plan(mandate)

        self.assertEqual(plan.max_capital_per_trade, 0.0)
        self.assertEqual(plan.max_symbol_allocation, 0.0)

    def test_live_execution_mode_is_preserved(self) -> None:
        mandate = make_mandate(execution_mode="live")
        plan = build_portfolio_allocation_plan(mandate)

        self.assertEqual(plan.execution_mode, "live")

    def test_build_from_loaded_mandate_file(self) -> None:
        mandate = load_daily_mandate(MANDATE_PATH)
        plan = build_portfolio_allocation_plan(mandate)

        self.assertEqual(plan.intraday_capital, mandate.buckets.intraday_bucket)
        self.assertEqual(plan.reserve_capital, mandate.buckets.reserve_bucket)
        self.assertEqual(plan.max_daily_loss, mandate.risk_limits.max_daily_loss)
        self.assertEqual(plan.max_trades_per_day, mandate.risk_limits.max_trades_per_day)
        self.assertEqual(plan.execution_mode, mandate.execution_mode)


class BuildPortfolioAllocationPlansTests(unittest.TestCase):
    def test_equal_weight_split_across_two_strategies(self) -> None:
        mandate = make_mandate(intraday_bucket=60000.0, max_daily_loss=2000.0, max_trades_per_day=10)

        plans = build_portfolio_allocation_plans(mandate, ["credit_spread", "momentum"])

        self.assertEqual(set(plans), {"credit_spread", "momentum"})
        for plan in plans.values():
            self.assertEqual(plan.intraday_capital, 30000.0)
            self.assertEqual(plan.max_daily_loss, 1000.0)
            self.assertEqual(plan.max_trades_per_day, 5)

    def test_capital_split_sums_back_to_the_mandate_total(self) -> None:
        mandate = make_mandate(reserve_bucket=40000.0, intraday_bucket=60000.0)

        plans = build_portfolio_allocation_plans(mandate, ["a", "b", "c"])

        self.assertAlmostEqual(sum(p.intraday_capital for p in plans.values()), 60000.0)
        self.assertAlmostEqual(sum(p.reserve_capital for p in plans.values()), 40000.0)

    def test_custom_weights_are_respected(self) -> None:
        mandate = make_mandate(intraday_bucket=100000.0)

        plans = build_portfolio_allocation_plans(
            mandate, ["a", "b"], weights={"a": 3.0, "b": 1.0}
        )

        self.assertEqual(plans["a"].intraday_capital, 75000.0)
        self.assertEqual(plans["b"].intraday_capital, 25000.0)

    def test_weights_need_not_sum_to_one(self) -> None:
        mandate = make_mandate(intraday_bucket=100000.0)

        plans = build_portfolio_allocation_plans(
            mandate, ["a", "b"], weights={"a": 30.0, "b": 10.0}
        )

        self.assertEqual(plans["a"].intraday_capital, 75000.0)
        self.assertEqual(plans["b"].intraday_capital, 25000.0)

    def test_kill_switch_thresholds_are_not_apportioned(self) -> None:
        mandate = make_mandate()
        mandate.risk_limits.max_consecutive_broker_errors = 3
        mandate.risk_limits.max_consecutive_order_rejections = 5

        plans = build_portfolio_allocation_plans(mandate, ["a", "b", "c"])

        for plan in plans.values():
            self.assertEqual(plan.max_consecutive_broker_errors, 3)
            self.assertEqual(plan.max_consecutive_order_rejections, 5)

    def test_single_strategy_matches_the_single_plan_builder(self) -> None:
        mandate = make_mandate()

        plans = build_portfolio_allocation_plans(mandate, ["only_one"])
        single_plan = build_portfolio_allocation_plan(mandate)

        self.assertEqual(plans["only_one"], single_plan)

    def test_max_trades_per_day_never_rounds_down_to_zero(self) -> None:
        mandate = make_mandate(max_trades_per_day=1)

        plans = build_portfolio_allocation_plans(mandate, ["a", "b", "c"])

        for plan in plans.values():
            self.assertGreaterEqual(plan.max_trades_per_day, 1)

    def test_empty_strategy_list_raises(self) -> None:
        mandate = make_mandate()

        with self.assertRaises(ValueError):
            build_portfolio_allocation_plans(mandate, [])

    def test_missing_weight_for_a_strategy_raises(self) -> None:
        mandate = make_mandate()

        with self.assertRaises(ValueError):
            build_portfolio_allocation_plans(mandate, ["a", "b"], weights={"a": 1.0})

    def test_original_mandate_is_not_mutated(self) -> None:
        mandate = make_mandate(intraday_bucket=60000.0)

        build_portfolio_allocation_plans(mandate, ["a", "b"])

        self.assertEqual(mandate.buckets.intraday_bucket, 60000.0)


if __name__ == "__main__":
    unittest.main()

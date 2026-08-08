"""
Unit tests for core.portfolio.optimizer.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_optimizer.py -v
"""
from __future__ import annotations

import unittest

import numpy as np

from core.portfolio.optimizer import compute_optimized_weights


class ComputeOptimizedWeightsTests(unittest.TestCase):
    def test_fewer_than_two_strategies_returns_none(self) -> None:
        self.assertIsNone(compute_optimized_weights({"only_one": [0.01, 0.02, 0.01, -0.01, 0.02]}))

    def test_misaligned_lengths_returns_none(self) -> None:
        returns = {"a": [0.01, 0.02, 0.01, -0.01], "b": [0.01, 0.02]}
        self.assertIsNone(compute_optimized_weights(returns))

    def test_too_few_observations_returns_none(self) -> None:
        returns = {"a": [0.01, 0.02], "b": [0.01, -0.02]}
        self.assertIsNone(compute_optimized_weights(returns))

    def test_a_strong_uncorrelated_strategy_gets_more_weight_than_a_noisy_losing_one(self) -> None:
        rng = np.random.default_rng(0)
        n = 250
        # Real edge, modest vol.
        strong = (rng.normal(0.001, 0.005, n)).tolist()
        # Negative expectancy, high vol, independent noise (not derived
        # from `strong`, so the two are genuinely uncorrelated rather than
        # just uncorrelated by construction coincidence).
        weak = (rng.normal(-0.0005, 0.02, n)).tolist()

        weights = compute_optimized_weights({"strong": strong, "weak": weak})

        self.assertIsNotNone(weights)
        self.assertGreater(weights["strong"], weights["weak"])

    def test_weights_sum_to_approximately_one(self) -> None:
        rng = np.random.default_rng(1)
        n = 250
        returns = {
            "a": rng.normal(0.0008, 0.01, n).tolist(),
            "b": rng.normal(0.0005, 0.012, n).tolist(),
            "c": rng.normal(0.0010, 0.015, n).tolist(),
        }

        weights = compute_optimized_weights(returns)

        self.assertIsNotNone(weights)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)

    def test_result_plugs_directly_into_build_portfolio_allocation_plans(self) -> None:
        # The whole point of this module: build_portfolio_allocation_plans
        # already accepts a `weights` dict and only defaults to 1/N when
        # one isn't given - this optimizer's output must be usable there
        # unmodified, not need its own adapter.
        from datetime import date

        from core.allocations.daily_mandate import (
            DailyCapitalBuckets,
            DailyMandate,
            DailyRiskLimits,
        )
        from core.portfolio.allocation import build_portfolio_allocation_plans

        rng = np.random.default_rng(2)
        n = 250
        returns = {
            "gap_continuation": rng.normal(0.001, 0.008, n).tolist(),
            "some_second_strategy": rng.normal(0.0004, 0.015, n).tolist(),
        }
        weights = compute_optimized_weights(returns)
        self.assertIsNotNone(weights)

        mandate = DailyMandate(
            trading_date=date(2026, 8, 8),
            buckets=DailyCapitalBuckets(reserve_bucket=20_000.0, intraday_bucket=80_000.0),
            risk_limits=DailyRiskLimits(max_daily_loss=5_000.0, max_trades_per_day=20,
                                         max_consecutive_broker_errors=3, max_consecutive_order_rejections=3),
            execution_mode="paper",
        )

        plans = build_portfolio_allocation_plans(mandate, list(returns.keys()), weights=weights)

        self.assertEqual(set(plans), set(returns.keys()))
        total_intraday = sum(plan.intraday_capital for plan in plans.values())
        self.assertAlmostEqual(total_intraday, mandate.buckets.intraday_bucket, places=6)


if __name__ == "__main__":
    unittest.main()

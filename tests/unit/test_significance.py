"""
Unit tests for core.backtest.significance.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_significance.py -v
"""
from __future__ import annotations

import unittest

from core.backtest.significance import (
    benjamini_hochberg,
    bootstrap_correlation_ci,
    bootstrap_mean_ci,
    bootstrap_profit_factor_ci,
    permutation_test_correlation,
    permutation_test_vs_zero,
    spearman_correlation,
)

# 35 wins of +50, 5 losses of -50 - PF=7, mean=+37.5: a real, strong edge.
_STRONG_EDGE_PNLS = [50.0] * 35 + [-50.0] * 5

# 20 wins of +10, 20 losses of -10 - PF=1.0 exactly, mean=0: no edge.
_ZERO_EDGE_PNLS = [10.0] * 20 + [-10.0] * 20


class BootstrapProfitFactorCiTests(unittest.TestCase):
    def test_strong_edge_rejects_null(self) -> None:
        ci = bootstrap_profit_factor_ci(_STRONG_EDGE_PNLS, seed=1)
        self.assertIsNotNone(ci.lower)
        self.assertGreater(ci.lower, 1.0)

    def test_zero_edge_does_not_reject_null(self) -> None:
        ci = bootstrap_profit_factor_ci(_ZERO_EDGE_PNLS, seed=1)
        self.assertIsNotNone(ci.lower)
        self.assertLessEqual(ci.lower, 1.0)

    def test_deterministic_given_same_seed(self) -> None:
        ci_a = bootstrap_profit_factor_ci(_STRONG_EDGE_PNLS, seed=42)
        ci_b = bootstrap_profit_factor_ci(_STRONG_EDGE_PNLS, seed=42)
        self.assertEqual(ci_a, ci_b)

    def test_too_few_values_returns_no_interval(self) -> None:
        ci = bootstrap_profit_factor_ci([10.0], seed=1)
        self.assertIsNone(ci.lower)
        self.assertIsNone(ci.upper)


class BootstrapMeanCiTests(unittest.TestCase):
    def test_clearly_positive_mean_rejects_zero(self) -> None:
        # Analogous to a VRP series with a real, consistent positive premium.
        ci = bootstrap_mean_ci([2.0] * 30 + [1.5] * 10, seed=1)
        self.assertIsNotNone(ci.lower)
        self.assertGreater(ci.lower, 0.0)

    def test_noisy_near_zero_mean_does_not_reject_zero(self) -> None:
        values = [1.0, -1.0] * 20
        ci = bootstrap_mean_ci(values, seed=1)
        self.assertIsNotNone(ci.lower)
        self.assertLessEqual(ci.lower, 0.0)


class PermutationTestVsZeroTests(unittest.TestCase):
    def test_strong_edge_is_significant(self) -> None:
        p_value = permutation_test_vs_zero(_STRONG_EDGE_PNLS, seed=1)
        self.assertLess(p_value, 0.01)

    def test_zero_edge_is_not_significant(self) -> None:
        p_value = permutation_test_vs_zero(_ZERO_EDGE_PNLS, seed=1)
        self.assertGreater(p_value, 0.5)

    def test_no_values_returns_none(self) -> None:
        self.assertIsNone(permutation_test_vs_zero([], seed=1))


class BenjaminiHochbergTests(unittest.TestCase):
    def test_lone_borderline_pvalue_survives_alone(self) -> None:
        survives = benjamini_hochberg({"strategy_a": 0.04})
        self.assertTrue(survives["strategy_a"])

    def test_same_pvalue_fails_once_tested_alongside_many_null_results(self) -> None:
        # Same strategy_a p-value as above (0.04, individually "significant"
        # at p<0.05) - but this platform has tested ~18 strategy configs, not
        # 1. Judged alongside 9 companions that show no edge, the correction
        # should withdraw significance rather than let one lucky p-value
        # through a flat threshold.
        p_values = {"strategy_a": 0.04}
        for i in range(9):
            p_values[f"null_strategy_{i}"] = 0.3 + i * 0.05
        survives = benjamini_hochberg(p_values)
        self.assertFalse(survives["strategy_a"])

    def test_all_clearly_significant_survive(self) -> None:
        survives = benjamini_hochberg({"a": 0.001, "b": 0.002, "c": 0.003})
        self.assertTrue(all(survives.values()))

    def test_all_clearly_null_fail(self) -> None:
        survives = benjamini_hochberg({"a": 0.5, "b": 0.6, "c": 0.7})
        self.assertFalse(any(survives.values()))


class SpearmanCorrelationTests(unittest.TestCase):
    def test_perfect_monotonic_relationship_is_one(self) -> None:
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertAlmostEqual(spearman_correlation(x, y), 1.0, places=6)

    def test_perfect_inverse_relationship_is_negative_one(self) -> None:
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [50.0, 40.0, 30.0, 20.0, 10.0]
        self.assertAlmostEqual(spearman_correlation(x, y), -1.0, places=6)

    def test_ties_use_average_rank(self) -> None:
        # x has a tie at rank 1.5 (both smallest); should not crash or
        # silently misrank - just verify it stays in [-1, 1] and computes.
        result = spearman_correlation([1.0, 1.0, 2.0, 3.0], [10.0, 20.0, 30.0, 40.0])
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result, -1.0)
        self.assertLessEqual(result, 1.0)

    def test_constant_series_returns_none(self) -> None:
        self.assertIsNone(spearman_correlation([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))

    def test_too_few_pairs_returns_none(self) -> None:
        self.assertIsNone(spearman_correlation([1.0], [2.0]))


class BootstrapCorrelationCiTests(unittest.TestCase):
    def test_strong_relationship_rejects_zero(self) -> None:
        x = list(range(30))
        y = [v * 2 + 1 for v in x]
        ci = bootstrap_correlation_ci(x, y, seed=1)
        self.assertIsNotNone(ci.lower)
        self.assertGreater(ci.lower, 0.0)

    def test_no_relationship_does_not_reject_zero(self) -> None:
        # Alternating so rank(x) and rank(y) are essentially unrelated.
        x = [i % 2 for i in range(30)]
        y = [(i * 7) % 5 for i in range(30)]
        ci = bootstrap_correlation_ci(x, y, seed=1)
        self.assertIsNotNone(ci.lower)
        self.assertLessEqual(ci.lower, 0.0)


class PermutationTestCorrelationTests(unittest.TestCase):
    def test_strong_relationship_is_significant(self) -> None:
        x = list(range(30))
        y = [v * 2 + 1 for v in x]
        p = permutation_test_correlation(x, y, seed=1)
        self.assertLess(p, 0.01)

    def test_no_relationship_is_not_significant(self) -> None:
        x = [i % 2 for i in range(30)]
        y = [(i * 7) % 5 for i in range(30)]
        p = permutation_test_correlation(x, y, seed=1)
        self.assertGreater(p, 0.1)


if __name__ == "__main__":
    unittest.main()

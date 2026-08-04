"""
Unit tests for core.risk.portfolio_var, against hand-computed fixtures
per the dashboard rebuild plan's Phase 4 verification step (this is
money-path logic - a real assert-based test, not a smoke test).

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_portfolio_var.py -v
"""
from __future__ import annotations

import unittest

from core.risk.portfolio_var import compute_beta, compute_historical_var


def _values_from_returns(start: float, returns: list[float]) -> list[float]:
    values = [start]
    for r in returns:
        values.append(values[-1] * (1 + r))
    return values


class ComputeHistoricalVarTests(unittest.TestCase):
    def test_hand_computed_var_and_es_at_90pct_confidence(self) -> None:
        # 20 returns, one clear worst day (-10%) and one clear
        # second-worst (-2%), rest small/flat - hand-verifiable tail.
        returns = [-0.10, -0.02] + [0.01] * 9 + [0.0] * 9
        values = _values_from_returns(100.0, returns)

        result = compute_historical_var(values, confidence_level=0.90)

        # cutoff_index = max(int((1-0.90)*20), 1) = 2 -> tail is the 2
        # worst returns: [-0.10, -0.02]. VaR = -returns[cutoff-1] = 2%.
        # ES = -mean(tail) = -mean([-0.10, -0.02]) = 6%.
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.value_at_risk_pct, 2.0, places=6)
        self.assertAlmostEqual(result.expected_shortfall_pct, 6.0, places=6)
        self.assertEqual(result.observation_count, 20)

    def test_es_is_at_least_as_large_as_var_always(self) -> None:
        # Expected Shortfall is the average of the tail INCLUDING the VaR
        # point itself, so it can never be smaller than VaR - a basic
        # sanity invariant worth asserting on any return distribution.
        returns = [-0.15, -0.08, -0.05, -0.03, -0.01] + [0.005] * 15
        values = _values_from_returns(100.0, returns)

        result = compute_historical_var(values, confidence_level=0.95)

        self.assertGreaterEqual(result.expected_shortfall_pct, result.value_at_risk_pct)

    def test_returns_none_below_minimum_observation_count(self) -> None:
        values = _values_from_returns(100.0, [0.01, -0.01, 0.02])  # only 3 returns

        self.assertIsNone(compute_historical_var(values))


class ComputeBetaTests(unittest.TestCase):
    def test_beta_of_one_when_portfolio_moves_identically_to_benchmark(self) -> None:
        bench_returns = [0.01, -0.02, 0.015, -0.005, 0.02] * 5  # 25 obs
        bench_values = _values_from_returns(100.0, bench_returns)
        portfolio_values = _values_from_returns(50.0, bench_returns)  # identical % moves

        result = compute_beta(portfolio_values, bench_values)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.beta, 1.0, places=9)

    def test_beta_of_two_when_portfolio_moves_twice_as_much(self) -> None:
        bench_returns = [0.01, -0.02, 0.015, -0.005, 0.02] * 5
        bench_values = _values_from_returns(100.0, bench_returns)
        portfolio_returns = [2 * r for r in bench_returns]
        portfolio_values = _values_from_returns(100.0, portfolio_returns)

        result = compute_beta(portfolio_values, bench_values)

        self.assertAlmostEqual(result.beta, 2.0, places=6)

    def test_returns_none_below_minimum_observation_count(self) -> None:
        bench_values = _values_from_returns(100.0, [0.01, -0.01])
        portfolio_values = _values_from_returns(100.0, [0.02, -0.02])

        self.assertIsNone(compute_beta(portfolio_values, bench_values))

    def test_returns_none_when_benchmark_has_zero_variance(self) -> None:
        flat_bench = [100.0] * 25
        portfolio_values = _values_from_returns(100.0, [0.01, -0.01] * 12 + [0.0])

        self.assertIsNone(compute_beta(portfolio_values, flat_bench))


if __name__ == "__main__":
    unittest.main()

"""
Unit tests for core.analytics.extended_ratios, against hand-computed
fixtures per the dashboard rebuild plan's Phase 5 verification step.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_extended_ratios.py -v
"""
from __future__ import annotations

import math
import unittest

from core.analytics.extended_ratios import (
    compute_alpha_and_treynor,
    compute_calmar_ratio,
    compute_information_ratio,
    compute_omega_ratio,
    compute_sortino_ratio,
)


def _values_from_returns(start: float, returns: list[float]) -> list[float]:
    values = [start]
    for r in returns:
        values.append(values[-1] * (1 + r))
    return values


class ComputeSortinoRatioTests(unittest.TestCase):
    def test_hand_computed_sortino_with_one_downside_observation(self) -> None:
        # 4 returns, exactly one below target(=0): downside=[0,0,0,-0.02].
        # downside_deviation = sqrt(0.02^2 / 4) = sqrt(0.0001) = 0.01 exactly.
        # mean_excess = (0.05*3 - 0.02) / 4 = 0.13/4 = 0.0325 exactly.
        returns = [0.05, 0.05, 0.05, -0.02]
        values = _values_from_returns(100.0, returns)

        result = compute_sortino_ratio(values, risk_free_rate=0.0)

        expected = (0.0325 / 0.01) * math.sqrt(252)
        self.assertAlmostEqual(result, expected, places=6)

    def test_none_when_no_downside_observations(self) -> None:
        values = _values_from_returns(100.0, [0.01, 0.02, 0.01, 0.03])

        self.assertIsNone(compute_sortino_ratio(values, risk_free_rate=0.0))


class ComputeCalmarRatioTests(unittest.TestCase):
    def test_divides_cagr_by_max_drawdown(self) -> None:
        self.assertAlmostEqual(compute_calmar_ratio(20.0, 10.0), 2.0, places=9)

    def test_none_when_cagr_unavailable(self) -> None:
        self.assertIsNone(compute_calmar_ratio(None, 10.0))

    def test_none_when_max_drawdown_is_zero(self) -> None:
        self.assertIsNone(compute_calmar_ratio(20.0, 0.0))


class ComputeOmegaRatioTests(unittest.TestCase):
    def test_hand_computed_omega_at_zero_threshold(self) -> None:
        # gains above 0: 0.04 + 0.02 = 0.06. losses below 0: 0.01 + 0.01 = 0.02.
        # Omega = 0.06 / 0.02 = 3.0 exactly.
        returns = [0.04, -0.01, 0.02, -0.01]
        values = _values_from_returns(100.0, returns)

        result = compute_omega_ratio(values, threshold=0.0)

        self.assertAlmostEqual(result, 3.0, places=9)

    def test_none_when_no_losses_below_threshold(self) -> None:
        values = _values_from_returns(100.0, [0.01, 0.02, 0.03])

        self.assertIsNone(compute_omega_ratio(values, threshold=0.0))


class ComputeAlphaAndTreynorTests(unittest.TestCase):
    def test_zero_alpha_when_portfolio_return_exactly_matches_capm_prediction(self) -> None:
        bench_returns = [0.01, -0.02, 0.015, -0.005, 0.02] * 5  # 25 obs, beta will be ~1
        bench_values = _values_from_returns(100.0, bench_returns)
        portfolio_values = _values_from_returns(50.0, bench_returns)  # identical returns -> beta=1.0

        # beta=1.0 -> CAPM-expected return == benchmark's own CAGR exactly,
        # so alpha should be ~0 when portfolio CAGR == benchmark CAGR.
        alpha, treynor = compute_alpha_and_treynor(
            portfolio_values, portfolio_cagr_pct=15.0, benchmark_values=bench_values, benchmark_cagr_pct=15.0,
            risk_free_rate=0.07,
        )

        self.assertAlmostEqual(alpha, 0.0, places=6)
        # Treynor = (15 - 7) / (1.0 * 100) = 0.08
        self.assertAlmostEqual(treynor, 0.08, places=6)

    def test_positive_alpha_when_portfolio_beats_capm_prediction(self) -> None:
        bench_returns = [0.01, -0.02, 0.015, -0.005, 0.02] * 5
        bench_values = _values_from_returns(100.0, bench_returns)
        portfolio_values = _values_from_returns(50.0, bench_returns)  # beta = 1.0

        # beta=1 -> expected = risk_free + 1*(bench_cagr - risk_free) = bench_cagr = 15.0
        # portfolio actually returned 20.0 -> alpha = 20 - 15 = 5.0 exactly.
        alpha, _ = compute_alpha_and_treynor(
            portfolio_values, portfolio_cagr_pct=20.0, benchmark_values=bench_values, benchmark_cagr_pct=15.0,
            risk_free_rate=0.07,
        )

        self.assertAlmostEqual(alpha, 5.0, places=6)

    def test_none_when_beta_not_computable(self) -> None:
        alpha, treynor = compute_alpha_and_treynor(
            [100.0, 101.0], portfolio_cagr_pct=10.0, benchmark_values=[100.0, 101.0], benchmark_cagr_pct=10.0,
        )

        self.assertIsNone(alpha)
        self.assertIsNone(treynor)


class ComputeInformationRatioTests(unittest.TestCase):
    def test_hand_computed_information_ratio(self) -> None:
        # Active return is a constant +0.01 every day (portfolio always
        # beats benchmark by exactly 1%) -> zero tracking-error variance
        # is the DEGENERATE case (handled separately below); use a
        # slightly varying active return so tracking error is nonzero
        # and hand-verifiable.
        bench_returns = [0.01, 0.02, 0.01, 0.02]
        port_returns = [0.02, 0.02, 0.03, 0.01]  # active: [0.01, 0.00, 0.02, -0.01]
        bench_values = _values_from_returns(100.0, bench_returns)
        port_values = _values_from_returns(100.0, port_returns)

        active = [0.01, 0.00, 0.02, -0.01]
        mean_active = sum(active) / 4
        variance = sum((a - mean_active) ** 2 for a in active) / 3
        expected = (mean_active / variance**0.5) * math.sqrt(252)

        result = compute_information_ratio(port_values, bench_values)

        self.assertAlmostEqual(result, expected, places=9)

    def test_none_when_active_return_is_perfectly_constant(self) -> None:
        bench_returns = [0.01, 0.02, 0.01, 0.02]
        port_returns = [0.02, 0.03, 0.02, 0.03]  # always exactly +1% -> zero tracking error
        bench_values = _values_from_returns(100.0, bench_returns)
        port_values = _values_from_returns(100.0, port_returns)

        self.assertIsNone(compute_information_ratio(port_values, bench_values))


if __name__ == "__main__":
    unittest.main()

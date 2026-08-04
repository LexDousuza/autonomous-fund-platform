"""
Unit tests for core.analytics.correlation, against hand-computed
fixtures per the dashboard rebuild plan's Phase 3 verification step.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_correlation.py -v
"""
from __future__ import annotations

import unittest

from core.analytics.correlation import compute_correlation_matrix


def _closes_from_returns(start: float, returns: list[float]) -> list[float]:
    closes = [start]
    for r in returns:
        closes.append(closes[-1] * (1 + r))
    return closes


def _series(closes: list[float]) -> list[tuple[str, float]]:
    return [(f"2026-01-{i + 1:02d}", close) for i, close in enumerate(closes)]


class ComputeCorrelationMatrixTests(unittest.TestCase):
    def test_identical_return_series_is_perfectly_correlated(self) -> None:
        returns_a = [0.1, -0.05, 0.02, 0.03]
        # B's returns are the SAME percentage moves as A, on a different
        # starting price - the whole point of using returns, not price
        # levels, for correlation.
        closes_by_symbol = {
            "A": _series(_closes_from_returns(100.0, returns_a)),
            "B": _series(_closes_from_returns(500.0, returns_a)),
        }

        result = compute_correlation_matrix(closes_by_symbol)

        self.assertEqual(result.symbols, ["A", "B"])
        a_idx, b_idx = 0, 1
        self.assertAlmostEqual(result.matrix[a_idx][b_idx], 1.0, places=9)
        self.assertAlmostEqual(result.matrix[b_idx][a_idx], 1.0, places=9)
        self.assertAlmostEqual(result.matrix[a_idx][a_idx], 1.0, places=9)

    def test_negated_return_series_is_perfectly_anti_correlated(self) -> None:
        returns_a = [0.1, -0.05, 0.02, 0.03]
        returns_c = [-r for r in returns_a]
        closes_by_symbol = {
            "A": _series(_closes_from_returns(100.0, returns_a)),
            "C": _series(_closes_from_returns(50.0, returns_c)),
        }

        result = compute_correlation_matrix(closes_by_symbol)

        a_idx, c_idx = result.symbols.index("A"), result.symbols.index("C")
        self.assertAlmostEqual(result.matrix[a_idx][c_idx], -1.0, places=9)

    def test_flat_price_series_yields_none_not_a_crash(self) -> None:
        flat = _series([100.0, 100.0, 100.0, 100.0])
        moving = _series(_closes_from_returns(100.0, [0.1, -0.05, 0.02]))
        closes_by_symbol = {"FLAT": flat, "MOVING": moving}

        result = compute_correlation_matrix(closes_by_symbol)

        flat_idx, moving_idx = result.symbols.index("FLAT"), result.symbols.index("MOVING")
        self.assertIsNone(result.matrix[flat_idx][moving_idx])
        self.assertIsNone(result.matrix[flat_idx][flat_idx])  # zero-variance self-correlation is undefined too

    def test_single_symbol_matrix_is_one_by_one(self) -> None:
        closes_by_symbol = {"A": _series(_closes_from_returns(100.0, [0.1, -0.05]))}

        result = compute_correlation_matrix(closes_by_symbol)

        self.assertEqual(result.symbols, ["A"])
        self.assertAlmostEqual(result.matrix[0][0], 1.0, places=9)


if __name__ == "__main__":
    unittest.main()

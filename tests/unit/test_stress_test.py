"""
Unit tests for core.risk.stress_test.compute_worst_day_stress_test.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_stress_test.py -v
"""
from __future__ import annotations

import unittest

from core.risk.stress_test import compute_worst_day_stress_test


class ComputeWorstDayStressTestTests(unittest.TestCase):
    def test_hand_computed_single_repetition(self) -> None:
        # returns: [0.02, -0.10, 0.01] - worst is -10%.
        # current value = 100 * 1.02 * 0.90 * 1.01 = 92.7180 (last close).
        values = [100.0, 102.0, 91.8, 92.718]

        scenarios = compute_worst_day_stress_test(values, repetitions=(1,))

        self.assertEqual(len(scenarios), 1)
        scenario = scenarios[0]
        self.assertAlmostEqual(scenario.shock_return_pct, -10.0, places=6)
        expected_resulting_value = 92.718 * 0.90
        self.assertAlmostEqual(scenario.resulting_value, expected_resulting_value, places=6)
        self.assertAlmostEqual(scenario.drawdown_pct, 10.0, places=6)

    def test_multiple_repetitions_compound(self) -> None:
        values = [100.0, 90.0]  # single -10% return, clean

        scenarios = compute_worst_day_stress_test(values, repetitions=(1, 2))

        by_reps = {s.repetitions: s for s in scenarios}
        # x1: 90 * 0.90 = 81.0 -> drawdown 10% from 90
        self.assertAlmostEqual(by_reps[1].resulting_value, 81.0, places=6)
        self.assertAlmostEqual(by_reps[1].drawdown_pct, 10.0, places=6)
        # x2: 90 * 0.90^2 = 72.9 -> drawdown 19% from 90
        self.assertAlmostEqual(by_reps[2].resulting_value, 72.9, places=6)
        self.assertAlmostEqual(by_reps[2].drawdown_pct, 19.0, places=6)

    def test_none_below_minimum_observations(self) -> None:
        self.assertIsNone(compute_worst_day_stress_test([100.0]))


if __name__ == "__main__":
    unittest.main()

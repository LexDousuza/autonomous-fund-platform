"""
Unit tests for scripts.check_capital_ladder_gate.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_check_capital_ladder_gate.py -v
"""
from __future__ import annotations

import unittest

from scripts.check_capital_ladder_gate import (
    check_forward_metrics_cleared,
    check_optimizer_wired_in,
    check_second_validated_strategy,
)


class CheckSecondValidatedStrategyTests(unittest.TestCase):
    def test_reports_not_met_against_real_repo_state(self) -> None:
        # Real repo state right now: exactly one active strategy
        # (gap_continuation) - the 50k->250k bar requires a second one.
        met, reason = check_second_validated_strategy()
        self.assertFalse(met)
        self.assertIn("1/1", reason)


class CheckOptimizerWiredInTests(unittest.TestCase):
    def test_reports_not_met_against_real_repo_state(self) -> None:
        # compute_optimized_weights exists but nothing passes weights= at
        # either live call site yet - see core/portfolio/optimizer.py's
        # own docstring on why that's deliberate for now.
        met, reason = check_optimizer_wired_in()
        self.assertFalse(met)
        self.assertIn("still using default 1/N", reason)


class CheckForwardMetricsClearedTests(unittest.TestCase):
    def test_reports_not_met_against_real_repo_state(self) -> None:
        # No gap_continuation forward trades exist yet in data/live_sessions/.
        met, reason = check_forward_metrics_cleared()
        self.assertFalse(met)
        self.assertIn("0/", reason)


if __name__ == "__main__":
    unittest.main()

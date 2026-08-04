"""
Unit tests for scripts.run_phase2_robustness_audit's credit_spread hold-length
plumbing.

Before this change, ``_credit_spread_window`` never forwarded a hold length
to ``run_credit_spread_backtest`` at all - every window in the Phase 2 audit
silently ran at the strategy's own default (5 days) no matter what a caller
wanted, per docs/research/validation_audit_phase2_longer_hold.md's need to
re-run the same battery at 10/15-day holds. These tests pin the wiring, not
backtest correctness (already covered by test_credit_spread_strategy.py and
friends) - they mock run_credit_spread_backtest/build_performance_report to
avoid the real ~56s CSV load.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_phase2_robustness_audit.py -v
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import date
from unittest.mock import patch

from scripts.run_phase2_robustness_audit import _credit_spread_window, credit_spread_holdout_and_walkforward


@dataclass
class _FakeReport:
    closed_trades: list = field(default_factory=list)


class CreditSpreadWindowHoldLengthTests(unittest.TestCase):
    def test_default_stays_5_days_backward_compatible(self) -> None:
        captured: dict = {}

        def fake_backtest(**kwargs):
            captured.update(kwargs)
            return []

        with patch("scripts.run_phase2_robustness_audit.run_credit_spread_backtest", side_effect=fake_backtest), \
                patch("scripts.run_phase2_robustness_audit.build_performance_report", return_value=_FakeReport()):
            _credit_spread_window(
                provider=object(), symbol="NIFTY", start=date(2024, 1, 1), end=date(2024, 12, 31), label="x",
            )

        self.assertEqual(captured.get("max_holding_days"), 5)

    def test_explicit_hold_length_is_forwarded(self) -> None:
        captured: dict = {}

        def fake_backtest(**kwargs):
            captured.update(kwargs)
            return []

        with patch("scripts.run_phase2_robustness_audit.run_credit_spread_backtest", side_effect=fake_backtest), \
                patch("scripts.run_phase2_robustness_audit.build_performance_report", return_value=_FakeReport()):
            _credit_spread_window(
                provider=object(), symbol="NIFTY", start=date(2024, 1, 1), end=date(2024, 12, 31), label="x",
                max_holding_days=15,
            )

        self.assertEqual(captured["max_holding_days"], 15)

    def test_holdout_and_walkforward_forwards_hold_length_to_every_window(self) -> None:
        calls: list = []

        def fake_backtest(**kwargs):
            calls.append(kwargs.get("max_holding_days"))
            return []

        with patch("scripts.run_phase2_robustness_audit.run_credit_spread_backtest", side_effect=fake_backtest), \
                patch("scripts.run_phase2_robustness_audit.build_underlying_bars", return_value=[]), \
                patch("scripts.run_phase2_robustness_audit.build_performance_report", return_value=_FakeReport()):
            credit_spread_holdout_and_walkforward(provider=object(), symbol="NIFTY", max_holding_days=10)

        # dev + holdout + 8 walk-forward years (2018-2025) = 10 windows.
        self.assertEqual(len(calls), 10)
        self.assertTrue(all(h == 10 for h in calls))

    def test_holdout_and_walkforward_default_is_still_5(self) -> None:
        calls: list = []

        def fake_backtest(**kwargs):
            calls.append(kwargs.get("max_holding_days"))
            return []

        with patch("scripts.run_phase2_robustness_audit.run_credit_spread_backtest", side_effect=fake_backtest), \
                patch("scripts.run_phase2_robustness_audit.build_underlying_bars", return_value=[]), \
                patch("scripts.run_phase2_robustness_audit.build_performance_report", return_value=_FakeReport()):
            credit_spread_holdout_and_walkforward(provider=object(), symbol="NIFTY")

        self.assertTrue(all(h == 5 for h in calls))


if __name__ == "__main__":
    unittest.main()

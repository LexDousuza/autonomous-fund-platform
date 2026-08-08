"""
Unit tests for core.backtest.fdr_ledger.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_fdr_ledger.py -v
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.backtest.fdr_ledger import load_ledger, record_and_correct


class RecordAndCorrectTests(unittest.TestCase):
    def test_lone_borderline_pvalue_survives_alone(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            survives = record_and_correct("FEAT-A", 0.04, "2026-08-08", path=path)
            self.assertTrue(survives["FEAT-A"])

    def test_new_entries_can_withdraw_significance_from_a_prior_one(self) -> None:
        # Same shape as test_significance.py's batch case, but built up
        # across separate record_and_correct calls the way the research
        # loop actually uses it - one hypothesis registered at a time,
        # not one dict handed in up front.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            record_and_correct("strategy_a", 0.04, "2026-08-01", path=path)
            survives = {}
            for i in range(9):
                survives = record_and_correct(f"null_strategy_{i}", 0.3 + i * 0.05, "2026-08-02", path=path)
            self.assertFalse(survives["strategy_a"])

    def test_ledger_persists_across_calls(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            record_and_correct("a", 0.01, "2026-08-08", path=path)
            record_and_correct("b", 0.02, "2026-08-08", path=path)
            self.assertEqual(set(load_ledger(path)), {"a", "b"})

    def test_re_registering_the_same_id_overwrites_not_duplicates(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            record_and_correct("a", 0.5, "2026-08-01", path=path)
            record_and_correct("a", 0.001, "2026-08-08", path=path)
            ledger = load_ledger(path)
            self.assertEqual(len(ledger), 1)
            self.assertEqual(ledger["a"]["p_value"], 0.001)

    def test_missing_ledger_file_starts_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "does_not_exist.json"
            self.assertEqual(load_ledger(path), {})


if __name__ == "__main__":
    unittest.main()

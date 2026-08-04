"""
Unit tests for core.allocations.approved_capital.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_approved_capital.py -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.allocations.approved_capital import CapitalNotApprovedError, load_approved_capital


class LoadApprovedCapitalTests(unittest.TestCase):
    def test_missing_file_raises(self) -> None:
        with self.assertRaises(CapitalNotApprovedError):
            load_approved_capital("does/not/exist.yaml")

    def test_positive_value_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_capital.yaml"
            path.write_text("approved_capital: 50000.0\n", encoding="utf-8")

            self.assertEqual(load_approved_capital(path), 50000.0)

    def test_zero_placeholder_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_capital.yaml"
            path.write_text("approved_capital: 0.0\n", encoding="utf-8")

            with self.assertRaises(CapitalNotApprovedError):
                load_approved_capital(path)

    def test_negative_value_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_capital.yaml"
            path.write_text("approved_capital: -1.0\n", encoding="utf-8")

            with self.assertRaises(CapitalNotApprovedError):
                load_approved_capital(path)

    def test_missing_key_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_capital.yaml"
            path.write_text("some_other_field: 1.0\n", encoding="utf-8")

            with self.assertRaises(CapitalNotApprovedError):
                load_approved_capital(path)

    def test_shipped_placeholder_file_is_not_approved(self) -> None:
        # The committed config/mandates/approved_capital.yaml ships with
        # 0.0 specifically so multi-strategy trading cannot start until a
        # human replaces it - this is the end-to-end proof that holds.
        with self.assertRaises(CapitalNotApprovedError):
            load_approved_capital("config/mandates/approved_capital.yaml")


if __name__ == "__main__":
    unittest.main()

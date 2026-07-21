"""
Unit tests for core.allocations.daily_mandate, focused on the
risk_appetite field.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_daily_mandate.py -v
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from core.allocations.daily_mandate import (
    RISK_APPETITES,
    DailyCapitalBuckets,
    DailyMandate,
    DailyRiskLimits,
    load_daily_mandate,
    update_daily_mandate_fields,
)

SAMPLE_MANDATE_PATH = "config/mandates/sample_mandate.yaml"


def _mandate(risk_appetite: str = "moderate") -> DailyMandate:
    return DailyMandate(
        trading_date=date(2026, 7, 7),
        buckets=DailyCapitalBuckets(reserve_bucket=40000.0, intraday_bucket=60000.0),
        risk_limits=DailyRiskLimits(max_daily_loss=2500.0, max_trades_per_day=6),
        risk_appetite=risk_appetite,
    )


class RiskAppetiteDefaultTests(unittest.TestCase):
    def test_defaults_to_moderate(self) -> None:
        mandate = DailyMandate(
            trading_date=date(2026, 7, 7),
            buckets=DailyCapitalBuckets(reserve_bucket=40000.0, intraday_bucket=60000.0),
            risk_limits=DailyRiskLimits(max_daily_loss=2500.0, max_trades_per_day=6),
        )
        self.assertEqual(mandate.risk_appetite, "moderate")

    def test_all_three_tiers_are_valid(self) -> None:
        for tier in ("conservative", "moderate", "aggressive"):
            _mandate(tier).validate()  # must not raise

    def test_unknown_tier_raises(self) -> None:
        with self.assertRaises(ValueError):
            _mandate("yolo").validate()

    def test_risk_appetites_constant_matches_validated_set(self) -> None:
        self.assertEqual(RISK_APPETITES, {"conservative", "moderate", "aggressive"})


class LoadDailyMandateRiskAppetiteTests(unittest.TestCase):
    def test_sample_mandate_file_parses_risk_appetite(self) -> None:
        mandate = load_daily_mandate(SAMPLE_MANDATE_PATH)
        self.assertEqual(mandate.risk_appetite, "moderate")

    def test_missing_risk_appetite_in_yaml_defaults_to_moderate(self) -> None:
        yaml_without_risk_appetite = """
trading_date: "2026-07-07"
buckets:
  reserve_bucket: 40000
  intraday_bucket: 60000
risk_limits:
  max_daily_loss: 2500
  max_trades_per_day: 6
execution_mode: "paper"
"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "mandate.yaml"
            path.write_text(yaml_without_risk_appetite, encoding="utf-8")

            mandate = load_daily_mandate(path)

        self.assertEqual(mandate.risk_appetite, "moderate")

    def test_invalid_risk_appetite_in_yaml_raises(self) -> None:
        yaml_with_bad_risk_appetite = """
trading_date: "2026-07-07"
buckets:
  reserve_bucket: 40000
  intraday_bucket: 60000
risk_limits:
  max_daily_loss: 2500
  max_trades_per_day: 6
execution_mode: "paper"
risk_appetite: "yolo"
"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "mandate.yaml"
            path.write_text(yaml_with_bad_risk_appetite, encoding="utf-8")

            with self.assertRaises(ValueError):
                load_daily_mandate(path)


class UpdateDailyMandateFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.path = Path(tmp_dir.name) / "mandate.yaml"
        self.path.write_text(
            'trading_date: "2026-07-07"\n\n'
            "buckets:\n  reserve_bucket: 40000\n  intraday_bucket: 60000\n\n"
            "risk_limits:\n  max_daily_loss: 2500\n  max_trades_per_day: 6\n"
            '  stop_new_entries_after: "14:45"\n  force_square_off_at: "15:20"\n\n'
            'execution_mode: "paper"\n\n'
            "# a comment that must survive\n"
            'risk_appetite: "moderate"\n',
            encoding="utf-8",
        )

    def test_updates_numeric_field_unquoted(self) -> None:
        update_daily_mandate_fields(self.path, {"intraday_bucket": 75000})

        mandate = load_daily_mandate(self.path)
        self.assertEqual(mandate.buckets.intraday_bucket, 75000.0)

    def test_updates_string_field_quoted(self) -> None:
        update_daily_mandate_fields(self.path, {"risk_appetite": "aggressive"})

        mandate = load_daily_mandate(self.path)
        self.assertEqual(mandate.risk_appetite, "aggressive")

    def test_updates_multiple_fields_at_once(self) -> None:
        update_daily_mandate_fields(
            self.path,
            {"max_daily_loss": 5000, "max_trades_per_day": 10, "force_square_off_at": "15:00"},
        )

        mandate = load_daily_mandate(self.path)
        self.assertEqual(mandate.risk_limits.max_daily_loss, 5000.0)
        self.assertEqual(mandate.risk_limits.max_trades_per_day, 10)
        self.assertEqual(mandate.risk_limits.force_square_off_at, "15:00")

    def test_preserves_comments_and_untouched_fields(self) -> None:
        update_daily_mandate_fields(self.path, {"intraday_bucket": 75000})

        text = self.path.read_text()
        self.assertIn("a comment that must survive", text)
        self.assertIn('trading_date: "2026-07-07"', text)
        self.assertIn("reserve_bucket: 40000", text)

    def test_unknown_field_raises_and_writes_nothing(self) -> None:
        original = self.path.read_text()

        with self.assertRaises(ValueError):
            update_daily_mandate_fields(self.path, {"not_a_real_field": 1})

        self.assertEqual(self.path.read_text(), original)

    def test_valid_fields_before_an_invalid_one_are_not_partially_written(self) -> None:
        original = self.path.read_text()

        with self.assertRaises(ValueError):
            update_daily_mandate_fields(
                self.path, {"intraday_bucket": 99999, "not_a_real_field": 1}
            )

        # first field would have changed the in-memory text, but nothing
        # is written to disk until every field validates
        self.assertEqual(self.path.read_text(), original)


if __name__ == "__main__":
    unittest.main()

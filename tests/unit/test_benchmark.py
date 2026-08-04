"""
Unit tests for core.analytics.benchmark.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_benchmark.py -v
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from core.analytics.benchmark import compute_return_series_metrics, load_india_vix_series, load_index_spot_series

_HEADER = (
    "trade_date,symbol,expiry,strike,option_type,settle_price,spot,"
    "time_to_expiry_years,risk_free_rate,implied_vol,open_interest,volume\n"
)


class ComputeReturnSeriesMetricsTests(unittest.TestCase):
    def test_total_return_and_cagr_for_simple_doubling(self) -> None:
        dates = [date(2025, 1, 1), date(2026, 1, 1)]
        metrics = compute_return_series_metrics(dates, [100.0, 200.0])

        self.assertAlmostEqual(metrics.total_return_pct, 100.0, places=3)
        self.assertAlmostEqual(metrics.cagr_pct, 100.0, delta=1.0)  # ~1 year elapsed

    def test_max_drawdown_from_peak(self) -> None:
        dates = [date(2026, 1, i) for i in range(1, 6)]
        # 100 -> 120 (peak) -> 90 (30 down from peak = 25%) -> 95 -> 110
        metrics = compute_return_series_metrics(dates, [100.0, 120.0, 90.0, 95.0, 110.0])

        self.assertAlmostEqual(metrics.max_drawdown_pct, 25.0, places=3)

    def test_requires_at_least_two_observations(self) -> None:
        with self.assertRaises(ValueError):
            compute_return_series_metrics([date(2026, 1, 1)], [100.0])

    def test_mismatched_lengths_raise(self) -> None:
        with self.assertRaises(ValueError):
            compute_return_series_metrics([date(2026, 1, 1), date(2026, 1, 2)], [100.0])


class LoadIndexSpotSeriesTests(unittest.TestCase):
    def _write_csv(self, rows: list[str]) -> Path:
        tmp_dir = Path(tempfile.mkdtemp())
        csv_path = tmp_dir / "iv_history.csv"
        csv_path.write_text(_HEADER + "".join(rows), encoding="utf-8")
        return csv_path

    def test_dedupes_to_one_point_per_date_and_filters_by_symbol(self) -> None:
        csv_path = self._write_csv(
            [
                "2026-01-05,NIFTY,2026-01-29,24000,PE,100,24000.0,0.05,0.07,0.15,0,0\n",
                "2026-01-05,NIFTY,2026-01-29,24100,CE,90,24000.0,0.05,0.07,0.14,0,0\n",
                "2026-01-06,NIFTY,2026-01-29,24000,PE,95,24150.5,0.04,0.07,0.15,0,0\n",
                "2026-01-05,BANKNIFTY,2026-01-29,52000,PE,300,52000.0,0.05,0.07,0.20,0,0\n",
            ]
        )

        series = load_index_spot_series(date(2026, 1, 1), date(2026, 1, 31), csv_path=csv_path)

        self.assertEqual(series, [(date(2026, 1, 5), 24000.0), (date(2026, 1, 6), 24150.5)])

    def test_filters_out_of_range_dates(self) -> None:
        csv_path = self._write_csv(
            [
                "2025-01-01,NIFTY,2025-01-29,24000,PE,100,20000.0,0.05,0.07,0.15,0,0\n",
                "2026-01-05,NIFTY,2026-01-29,24000,PE,100,24000.0,0.05,0.07,0.15,0,0\n",
            ]
        )

        series = load_index_spot_series(date(2026, 1, 1), date(2026, 1, 31), csv_path=csv_path)

        self.assertEqual(series, [(date(2026, 1, 5), 24000.0)])

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_index_spot_series(date(2026, 1, 1), date(2026, 1, 31), csv_path="does/not/exist.csv")

    def test_no_matching_rows_raises(self) -> None:
        csv_path = self._write_csv(["2026-01-05,BANKNIFTY,2026-01-29,52000,PE,300,52000.0,0.05,0.07,0.20,0,0\n"])

        with self.assertRaises(ValueError):
            load_index_spot_series(date(2026, 1, 1), date(2026, 1, 31), symbol="NIFTY", csv_path=csv_path)


class LoadIndiaVixSeriesTests(unittest.TestCase):
    _HEADER = "symbol,exchange,timestamp,open,high,low,close,volume\n"

    def _write_csv(self, rows: list[str]) -> Path:
        tmp_dir = Path(tempfile.mkdtemp())
        csv_path = tmp_dir / "india_vix_daily.csv"
        csv_path.write_text(self._HEADER + "".join(rows), encoding="utf-8")
        return csv_path

    def test_reads_one_point_per_day_and_filters_by_range(self) -> None:
        csv_path = self._write_csv(
            [
                "INDIA VIX,NSE,2026-01-05 00:00:00,14.0,14.5,13.5,14.2,0\n",
                "INDIA VIX,NSE,2026-01-06 00:00:00,14.2,15.0,14.0,14.8,0\n",
                "INDIA VIX,NSE,2025-01-01 00:00:00,12.0,12.5,11.5,12.1,0\n",
            ]
        )

        series = load_india_vix_series(date(2026, 1, 1), date(2026, 1, 31), csv_path=csv_path)

        self.assertEqual(series, [(date(2026, 1, 5), 14.2), (date(2026, 1, 6), 14.8)])

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_india_vix_series(date(2026, 1, 1), date(2026, 1, 31), csv_path="does/not/exist.csv")

    def test_no_matching_rows_raises(self) -> None:
        csv_path = self._write_csv(["INDIA VIX,NSE,2020-01-05 00:00:00,14.0,14.5,13.5,14.2,0\n"])

        with self.assertRaises(ValueError):
            load_india_vix_series(date(2026, 1, 1), date(2026, 1, 31), csv_path=csv_path)


if __name__ == "__main__":
    unittest.main()

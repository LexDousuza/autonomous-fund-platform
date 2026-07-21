"""
Unit tests for core.backtest.synthetic_data.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_synthetic_data.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, time

from core.backtest.synthetic_data import SyntheticSymbolConfig, generate_synthetic_bars

RELIANCE = SyntheticSymbolConfig(symbol="RELIANCE", exchange="NSE", starting_price=2500.0)
TCS = SyntheticSymbolConfig(symbol="TCS", exchange="NSE", starting_price=3800.0)


class GenerateSyntheticBarsTests(unittest.TestCase):
    def test_same_seed_produces_identical_bars(self) -> None:
        bars_a = generate_synthetic_bars([RELIANCE], date(2026, 1, 5), num_days=3, seed=7)
        bars_b = generate_synthetic_bars([RELIANCE], date(2026, 1, 5), num_days=3, seed=7)

        self.assertEqual(bars_a, bars_b)

    def test_different_seed_produces_different_bars(self) -> None:
        bars_a = generate_synthetic_bars([RELIANCE], date(2026, 1, 5), num_days=3, seed=1)
        bars_b = generate_synthetic_bars([RELIANCE], date(2026, 1, 5), num_days=3, seed=2)

        self.assertNotEqual([b.close for b in bars_a], [b.close for b in bars_b])

    def test_covers_only_weekdays(self) -> None:
        # 2026-01-05 is a Monday; 7 trading days should span a weekend
        # (skip Sat 01-10 and Sun 01-11), landing on 2026-01-13.
        bars = generate_synthetic_bars([RELIANCE], date(2026, 1, 5), num_days=7)

        trading_dates = sorted({bar.timestamp.date() for bar in bars})
        self.assertEqual(len(trading_dates), 7)
        for trading_date in trading_dates:
            self.assertLess(trading_date.weekday(), 5)
        self.assertEqual(trading_dates[-1], date(2026, 1, 13))

    def test_bars_stay_within_market_hours(self) -> None:
        bars = generate_synthetic_bars([RELIANCE], date(2026, 1, 5), num_days=1, interval_minutes=5)

        for bar in bars:
            self.assertGreaterEqual(bar.timestamp.time(), time(9, 15))
            self.assertLessEqual(bar.timestamp.time(), time(15, 30))

    def test_multi_symbol_output_is_chronologically_interleaved(self) -> None:
        bars = generate_synthetic_bars([RELIANCE, TCS], date(2026, 1, 5), num_days=1, interval_minutes=30)

        timestamps = [bar.timestamp for bar in bars]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual({bar.symbol for bar in bars}, {"RELIANCE", "TCS"})

    def test_ohlc_relationships_are_always_valid(self) -> None:
        bars = generate_synthetic_bars([RELIANCE, TCS], date(2026, 1, 5), num_days=5, interval_minutes=5, seed=99)

        for bar in bars:
            self.assertGreaterEqual(bar.high, max(bar.open, bar.close))
            self.assertLessEqual(bar.low, min(bar.open, bar.close))
            self.assertGreater(bar.close, 0)
            self.assertGreater(bar.volume, 0)

    def test_zero_or_negative_num_days_raises(self) -> None:
        with self.assertRaises(ValueError):
            generate_synthetic_bars([RELIANCE], date(2026, 1, 5), num_days=0)


class MomentumTests(unittest.TestCase):
    def test_zero_momentum_is_deterministic_and_unaffected(self) -> None:
        no_momentum = SyntheticSymbolConfig(
            symbol="RELIANCE", exchange="NSE", starting_price=2500.0, momentum_strength=0.0
        )
        bars_a = generate_synthetic_bars([no_momentum], date(2026, 1, 5), num_days=2, seed=5)
        bars_b = generate_synthetic_bars([no_momentum], date(2026, 1, 5), num_days=2, seed=5)

        self.assertEqual(bars_a, bars_b)

    def test_positive_momentum_changes_the_series_but_stays_deterministic(self) -> None:
        no_momentum = SyntheticSymbolConfig(
            symbol="RELIANCE", exchange="NSE", starting_price=2500.0, momentum_strength=0.0
        )
        with_momentum = SyntheticSymbolConfig(
            symbol="RELIANCE", exchange="NSE", starting_price=2500.0, momentum_strength=0.3
        )

        baseline = generate_synthetic_bars([no_momentum], date(2026, 1, 5), num_days=2, seed=5)
        momentum_a = generate_synthetic_bars([with_momentum], date(2026, 1, 5), num_days=2, seed=5)
        momentum_b = generate_synthetic_bars([with_momentum], date(2026, 1, 5), num_days=2, seed=5)

        self.assertEqual(momentum_a, momentum_b)  # still deterministic
        self.assertNotEqual([b.close for b in baseline], [b.close for b in momentum_a])

    def test_momentum_still_produces_valid_ohlc(self) -> None:
        with_momentum = SyntheticSymbolConfig(
            symbol="RELIANCE", exchange="NSE", starting_price=2500.0, momentum_strength=0.4
        )
        bars = generate_synthetic_bars([with_momentum], date(2026, 1, 5), num_days=3, seed=11)

        for bar in bars:
            self.assertGreaterEqual(bar.high, max(bar.open, bar.close))
            self.assertLessEqual(bar.low, min(bar.open, bar.close))


if __name__ == "__main__":
    unittest.main()

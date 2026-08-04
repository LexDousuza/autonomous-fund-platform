"""
Unit tests for core.market_data.memory_feed.MemoryMarketDataFeed, focused
on seed_synthetic_price_path - the paper-mode stopgap that lets a
dynamically-subscribed symbol (e.g. a naked write's option contract)
actually stream bars, so the exit engine can run against it.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_memory_feed.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from core.market_data.memory_feed import MemoryMarketDataFeed
from core.strategy.base import MarketBar


def make_bars(symbol: str, closes: list[float], start: datetime) -> list[MarketBar]:
    return [
        MarketBar(
            symbol=symbol, exchange="NSE", timestamp=start + timedelta(minutes=i),
            open=c, high=c, low=c, close=c, volume=1000.0,
        )
        for i, c in enumerate(closes)
    ]


class SeedSyntheticPricePathTests(unittest.TestCase):
    def test_seeds_a_bar_for_every_existing_timestamp_after_the_cutoff(self) -> None:
        start = datetime(2026, 1, 1, 9, 15)
        underlying_bars = make_bars("NIFTY", [100.0, 101.0, 102.0, 103.0], start)
        feed = MemoryMarketDataFeed(bars=underlying_bars)

        feed.seed_synthetic_price_path(
            "NIFTY_PE_18000", "NFO", after_timestamp=start + timedelta(minutes=1), seed_price=50.0
        )

        option_history = feed.get_history("NIFTY_PE_18000")
        # Only timestamps strictly after the cutoff (minutes 2 and 3), not
        # minutes 0 or 1.
        self.assertEqual(len(option_history), 2)
        self.assertEqual([b.timestamp for b in option_history], [start + timedelta(minutes=2), start + timedelta(minutes=3)])
        self.assertTrue(all(b.close == 50.0 for b in option_history))

    def test_price_path_overrides_flat_seed_price(self) -> None:
        start = datetime(2026, 1, 1, 9, 15)
        underlying_bars = make_bars("NIFTY", [100.0, 101.0, 102.0], start)
        feed = MemoryMarketDataFeed(bars=underlying_bars)

        feed.seed_synthetic_price_path(
            "NIFTY_PE_18000", "NFO", after_timestamp=start, seed_price=50.0, price_path=[60.0, 70.0],
        )

        closes = [b.close for b in feed.get_history("NIFTY_PE_18000")]
        self.assertEqual(closes, [60.0, 70.0])

    def test_price_path_shorter_than_timestamps_holds_its_last_value(self) -> None:
        start = datetime(2026, 1, 1, 9, 15)
        underlying_bars = make_bars("NIFTY", [100.0, 101.0, 102.0, 103.0], start)
        feed = MemoryMarketDataFeed(bars=underlying_bars)

        feed.seed_synthetic_price_path(
            "NIFTY_PE_18000", "NFO", after_timestamp=start, seed_price=50.0, price_path=[90.0],
        )

        closes = [b.close for b in feed.get_history("NIFTY_PE_18000")]
        self.assertEqual(closes, [90.0, 90.0, 90.0])

    def test_never_overwrites_a_symbol_that_already_has_real_bars(self) -> None:
        start = datetime(2026, 1, 1, 9, 15)
        real_option_bars = make_bars("NIFTY_PE_18000", [55.0, 56.0], start)
        feed = MemoryMarketDataFeed(bars=real_option_bars)

        feed.seed_synthetic_price_path("NIFTY_PE_18000", "NFO", after_timestamp=start, seed_price=999.0)

        closes = [b.close for b in feed.get_history("NIFTY_PE_18000")]
        self.assertEqual(closes, [55.0, 56.0])  # untouched, not appended to or replaced

    def test_seeded_bars_are_streamed_once_subscribed(self) -> None:
        start = datetime(2026, 1, 1, 9, 15)
        underlying_bars = make_bars("NIFTY", [100.0, 101.0], start)
        feed = MemoryMarketDataFeed(bars=underlying_bars)
        feed.subscribe("NIFTY")

        feed.seed_synthetic_price_path("NIFTY_PE_18000", "NFO", after_timestamp=start, seed_price=50.0)
        feed.subscribe("NIFTY_PE_18000")

        streamed_symbols = [b.symbol for b in feed.stream()]
        self.assertIn("NIFTY_PE_18000", streamed_symbols)


if __name__ == "__main__":
    unittest.main()

"""
Unit tests for core.market_data.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_market_data.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from core.market_data.csv_feed import CSVMarketDataFeed
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.strategy.base import MarketBar

CSV_PATH = "data/backtests/sample_reliance_1m.csv"


def make_bar(symbol: str, minute: int, close: float) -> MarketBar:
    base_time = datetime(2026, 1, 1, 9, 15, 0)
    return MarketBar(
        symbol=symbol,
        exchange="NSE",
        timestamp=base_time + timedelta(minutes=minute),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000.0,
    )


class CSVMarketDataFeedTests(unittest.TestCase):
    def test_get_history_returns_bars_for_symbol(self) -> None:
        feed = CSVMarketDataFeed(CSV_PATH)
        bars = feed.get_history("RELIANCE")

        self.assertGreater(len(bars), 0)
        self.assertTrue(all(bar.symbol == "RELIANCE" for bar in bars))

    def test_get_history_unknown_symbol_is_empty(self) -> None:
        feed = CSVMarketDataFeed(CSV_PATH)
        self.assertEqual(feed.get_history("UNKNOWN"), [])

    def test_stream_without_subscription_yields_all_bars(self) -> None:
        feed = CSVMarketDataFeed(CSV_PATH)
        streamed = list(feed.stream())
        self.assertEqual(streamed, feed.get_history("RELIANCE"))

    def test_stream_filters_to_subscribed_symbols(self) -> None:
        feed = CSVMarketDataFeed(CSV_PATH)
        feed.subscribe("UNKNOWN")
        self.assertEqual(list(feed.stream()), [])

    def test_get_latest_bar_updates_while_streaming(self) -> None:
        feed = CSVMarketDataFeed(CSV_PATH)
        self.assertIsNone(feed.get_latest_bar("RELIANCE"))

        bars = list(feed.stream())
        self.assertEqual(feed.get_latest_bar("RELIANCE"), bars[-1])

    def test_unsubscribe_removes_symbol_from_stream(self) -> None:
        feed = CSVMarketDataFeed(CSV_PATH)
        feed.subscribe("RELIANCE")
        feed.unsubscribe("RELIANCE")
        # Once nothing is explicitly subscribed the feed falls back to
        # streaming everything (same semantics as a fresh feed).
        self.assertEqual(list(feed.stream()), feed.get_history("RELIANCE"))


class MemoryMarketDataFeedTests(unittest.TestCase):
    def test_add_bar_updates_history_and_latest(self) -> None:
        feed = MemoryMarketDataFeed()
        bar1 = make_bar("TCS", 0, 100.0)
        bar2 = make_bar("TCS", 1, 101.0)

        feed.add_bar(bar1)
        feed.add_bar(bar2)

        self.assertEqual(feed.get_history("TCS"), [bar1, bar2])
        self.assertEqual(feed.get_latest_bar("TCS"), bar2)

    def test_constructor_accepts_initial_bars(self) -> None:
        bar = make_bar("INFY", 0, 50.0)
        feed = MemoryMarketDataFeed(bars=[bar])

        self.assertEqual(feed.get_latest_bar("INFY"), bar)
        self.assertEqual(feed.get_history("INFY"), [bar])

    def test_stream_respects_subscription(self) -> None:
        feed = MemoryMarketDataFeed()
        feed.add_bar(make_bar("TCS", 0, 100.0))
        feed.add_bar(make_bar("INFY", 0, 50.0))

        feed.subscribe("TCS")
        streamed = list(feed.stream())

        self.assertEqual(len(streamed), 1)
        self.assertEqual(streamed[0].symbol, "TCS")

    def test_get_latest_bar_unknown_symbol_is_none(self) -> None:
        feed = MemoryMarketDataFeed()
        self.assertIsNone(feed.get_latest_bar("UNKNOWN"))


class MarketDataServiceTests(unittest.TestCase):
    def test_register_feed_makes_it_active_by_default(self) -> None:
        service = MarketDataService()
        feed = MemoryMarketDataFeed(bars=[make_bar("TCS", 0, 100.0)])

        service.register_feed("mem", feed)

        self.assertEqual(service.available_feeds(), ["mem"])
        self.assertIs(service.active_feed, feed)

    def test_register_duplicate_feed_name_raises(self) -> None:
        service = MarketDataService()
        service.register_feed("mem", MemoryMarketDataFeed())

        with self.assertRaises(ValueError):
            service.register_feed("mem", MemoryMarketDataFeed())

    def test_set_active_feed_switches_source(self) -> None:
        service = MarketDataService()
        bar_a = make_bar("TCS", 0, 100.0)
        bar_b = make_bar("TCS", 0, 200.0)

        service.register_feed("a", MemoryMarketDataFeed(bars=[bar_a]))
        service.register_feed("b", MemoryMarketDataFeed(bars=[bar_b]))

        self.assertEqual(service.get_latest_bar("TCS"), bar_a)

        service.set_active_feed("b")
        self.assertEqual(service.get_latest_bar("TCS"), bar_b)

    def test_set_active_feed_unknown_name_raises(self) -> None:
        service = MarketDataService()
        service.register_feed("mem", MemoryMarketDataFeed())

        with self.assertRaises(ValueError):
            service.set_active_feed("missing")

    def test_active_feed_without_registration_raises(self) -> None:
        service = MarketDataService()
        with self.assertRaises(ValueError):
            _ = service.active_feed

    def test_service_delegates_subscribe_and_stream(self) -> None:
        service = MarketDataService()
        service.register_feed(
            "mem",
            MemoryMarketDataFeed(bars=[make_bar("TCS", 0, 100.0), make_bar("INFY", 0, 50.0)]),
        )

        service.subscribe("TCS")
        streamed = list(service.stream())

        self.assertEqual(len(streamed), 1)
        self.assertEqual(streamed[0].symbol, "TCS")

    def test_service_with_csv_feed_matches_direct_history(self) -> None:
        service = MarketDataService()
        service.register_feed("csv", CSVMarketDataFeed(CSV_PATH))

        self.assertEqual(
            service.get_history("RELIANCE"),
            CSVMarketDataFeed(CSV_PATH).get_history("RELIANCE"),
        )


if __name__ == "__main__":
    unittest.main()

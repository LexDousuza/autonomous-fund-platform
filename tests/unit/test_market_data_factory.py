"""
Unit tests for core.market_data.factory.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_market_data_factory.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime

from core.market_data.csv_feed import CSVMarketDataFeed
from core.market_data.factory import build_market_data_feed
from core.market_data.factory_models import MarketDataConfig
from core.market_data.groww_live_feed import GrowwLiveMarketDataFeed
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.strategy.base import MarketBar

CSV_PATH = "data/backtests/sample_reliance_1m.csv"


def make_bar(symbol: str = "AAA", close: float = 100.0) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        exchange="NSE",
        timestamp=datetime(2026, 1, 1, 9, 15, 0),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000.0,
    )


class BuildMarketDataFeedTests(unittest.TestCase):
    def test_memory_config_returns_memory_feed(self) -> None:
        feed = build_market_data_feed(MarketDataConfig(provider="memory", mode="paper"))

        self.assertIsInstance(feed, MemoryMarketDataFeed)

    def test_memory_config_seeds_bars_from_metadata(self) -> None:
        bar = make_bar()
        feed = build_market_data_feed(
            MarketDataConfig(provider="memory", mode="paper", metadata={"bars": [bar]})
        )

        self.assertEqual(feed.get_latest_bar("AAA"), bar)

    def test_csv_config_returns_csv_feed(self) -> None:
        feed = build_market_data_feed(
            MarketDataConfig(provider="csv", mode="paper", metadata={"path": CSV_PATH})
        )

        self.assertIsInstance(feed, CSVMarketDataFeed)
        self.assertGreater(len(feed.get_history("RELIANCE")), 0)

    def test_csv_config_without_path_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(MarketDataConfig(provider="csv", mode="paper"))

    def test_groww_live_config_returns_groww_live_feed(self) -> None:
        feed = build_market_data_feed(
            MarketDataConfig(
                provider="groww",
                mode="live",
                metadata={"api_key": "key", "access_token": "token"},
            )
        )

        self.assertIsInstance(feed, GrowwLiveMarketDataFeed)

    def test_groww_paper_mode_raises(self) -> None:
        # V1: no verified separate Groww paper/sandbox market-data mode.
        with self.assertRaises(ValueError):
            build_market_data_feed(
                MarketDataConfig(
                    provider="groww",
                    mode="paper",
                    metadata={"api_key": "key", "access_token": "token"},
                )
            )

    def test_groww_without_credentials_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(MarketDataConfig(provider="groww", mode="live"))

    def test_groww_config_passes_through_settings(self) -> None:
        feed = build_market_data_feed(
            MarketDataConfig(
                provider="groww",
                mode="live",
                metadata={
                    "api_key": "key",
                    "access_token": "token",
                    "base_url": "https://api.groww.example",
                    "exchange": "BSE",
                    "poll_interval_seconds": 1.5,
                    "max_polls": 3,
                    "timeout_seconds": 20.0,
                },
            )
        )

        self.assertEqual(feed._exchange, "BSE")
        self.assertEqual(feed._settings.poll_interval_seconds, 1.5)
        self.assertEqual(feed._settings.max_polls, 3)
        self.assertEqual(feed._transport._config.timeout_seconds, 20.0)
        self.assertEqual(feed._transport._config.base_url, "https://api.groww.example")

    def test_groww_config_uses_safe_default_bounded_polls(self) -> None:
        feed = build_market_data_feed(
            MarketDataConfig(
                provider="groww",
                mode="live",
                metadata={"api_key": "key", "access_token": "token"},
            )
        )

        self.assertEqual(feed._settings.max_polls, 1)

    def test_unsupported_provider_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(MarketDataConfig(provider="polygon", mode="live"))

    def test_unsupported_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(MarketDataConfig(provider="memory", mode="sandbox"))

    def test_provider_and_mode_are_case_insensitive(self) -> None:
        feed = build_market_data_feed(MarketDataConfig(provider="MEMORY", mode="Paper"))

        self.assertIsInstance(feed, MemoryMarketDataFeed)


if __name__ == "__main__":
    unittest.main()

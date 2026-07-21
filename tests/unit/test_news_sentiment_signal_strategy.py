"""
Unit tests for core.strategy.news_sentiment_signal.

A fake news feed double is injected via StrategyContext.parameters -
never touches the real network or core.news.google_news_feed.GoogleNewsFeed.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_news_sentiment_signal_strategy.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from core.news.google_news_feed import NewsItem
from core.strategy.base import MarketBar, SignalSide, StrategyContext
from core.strategy.news_sentiment_signal import NewsSentimentSignalStrategy


class FakeNewsFeed:
    """Records every query it was asked for and returns canned items per-query."""

    def __init__(self, items_by_query: dict[str, list[NewsItem]] | None = None) -> None:
        self.items_by_query = items_by_query or {}
        self.calls: list[tuple[str, int]] = []

    def fetch(self, query: str, max_items: int = 10) -> list[NewsItem]:
        self.calls.append((query, max_items))
        return self.items_by_query.get(query, [])


def _bullish_item(headline: str = "Stock surges on record profit growth") -> NewsItem:
    return NewsItem(query="RELIANCE", headline=headline, link="", source=None, published_at=None)


def _neutral_item() -> NewsItem:
    return NewsItem(query="RELIANCE", headline="Company holds AGM next week", link="", source=None, published_at=None)


def _bar(symbol: str = "RELIANCE", minute: int = 0) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        exchange="NSE",
        timestamp=datetime(2026, 1, 5, 9, 15) + timedelta(minutes=minute),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10000,
    )


def _history(length: int) -> list[MarketBar]:
    return [_bar(minute=i) for i in range(length)]


def _context(**params: object) -> StrategyContext:
    return StrategyContext(strategy_name="news_sentiment_signal", parameters=params)


class NewsSentimentSignalStrategyTests(unittest.TestCase):
    def test_emits_buy_when_score_meets_threshold(self) -> None:
        feed = FakeNewsFeed({"RELIANCE NSE share price": [_bullish_item(), _bullish_item("Rally continues on strong growth")]})
        strategy = NewsSentimentSignalStrategy(_context(news_feed=feed, buy_score_threshold=2))

        signals = strategy.on_bar(_bar(), history=[], open_positions={})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, SignalSide.BUY)
        self.assertEqual(signals[0].symbol, "RELIANCE")
        self.assertEqual(signals[0].reason, "net_bullish_news_coverage")
        self.assertGreaterEqual(signals[0].metadata["news_score"], 2)

    def test_no_signal_when_score_below_threshold(self) -> None:
        feed = FakeNewsFeed({"RELIANCE NSE share price": [_neutral_item()]})
        strategy = NewsSentimentSignalStrategy(_context(news_feed=feed, buy_score_threshold=2))

        signals = strategy.on_bar(_bar(), history=[], open_positions={})

        self.assertEqual(signals, [])

    def test_skips_when_already_in_a_position(self) -> None:
        feed = FakeNewsFeed({"RELIANCE NSE share price": [_bullish_item(), _bullish_item()]})
        strategy = NewsSentimentSignalStrategy(_context(news_feed=feed, buy_score_threshold=2))

        signals = strategy.on_bar(_bar(), history=[], open_positions={"NSE:RELIANCE": object()})

        self.assertEqual(signals, [])
        self.assertEqual(feed.calls, [])  # never even checked the news

    def test_uses_custom_query_template(self) -> None:
        feed = FakeNewsFeed()
        strategy = NewsSentimentSignalStrategy(
            _context(news_feed=feed, query_template="{symbol} quarterly results")
        )

        strategy.on_bar(_bar(), history=[], open_positions={})

        self.assertEqual(feed.calls, [("RELIANCE quarterly results", 10)])

    def test_respects_max_headlines_param(self) -> None:
        feed = FakeNewsFeed()
        strategy = NewsSentimentSignalStrategy(_context(news_feed=feed, max_headlines=3))

        strategy.on_bar(_bar(), history=[], open_positions={})

        self.assertEqual(feed.calls, [("RELIANCE NSE share price", 3)])

    def test_cooldown_blocks_a_second_check_within_the_refresh_window(self) -> None:
        feed = FakeNewsFeed({"RELIANCE NSE share price": [_neutral_item()]})
        strategy = NewsSentimentSignalStrategy(_context(news_feed=feed, refresh_interval_bars=5))

        strategy.on_bar(_bar(minute=0), history=_history(0), open_positions={})
        strategy.on_bar(_bar(minute=1), history=_history(1), open_positions={})
        strategy.on_bar(_bar(minute=2), history=_history(2), open_positions={})

        self.assertEqual(len(feed.calls), 1)  # only the first call went through

    def test_checks_again_once_the_refresh_window_elapses(self) -> None:
        feed = FakeNewsFeed({"RELIANCE NSE share price": [_neutral_item()]})
        strategy = NewsSentimentSignalStrategy(_context(news_feed=feed, refresh_interval_bars=3))

        strategy.on_bar(_bar(minute=0), history=_history(0), open_positions={})  # bar count 1
        strategy.on_bar(_bar(minute=1), history=_history(1), open_positions={})  # bar count 2, blocked
        strategy.on_bar(_bar(minute=2), history=_history(2), open_positions={})  # bar count 3, blocked
        strategy.on_bar(_bar(minute=3), history=_history(3), open_positions={})  # bar count 4, allowed

        self.assertEqual(len(feed.calls), 2)

    def test_cooldown_is_tracked_independently_per_symbol(self) -> None:
        feed = FakeNewsFeed()
        strategy = NewsSentimentSignalStrategy(_context(news_feed=feed, refresh_interval_bars=5))

        strategy.on_bar(_bar(symbol="RELIANCE"), history=[], open_positions={})
        strategy.on_bar(_bar(symbol="TCS"), history=[], open_positions={})

        queries = {query for query, _ in feed.calls}
        self.assertEqual(queries, {"RELIANCE NSE share price", "TCS NSE share price"})

    def test_invalid_refresh_interval_raises(self) -> None:
        strategy = NewsSentimentSignalStrategy(_context(news_feed=FakeNewsFeed(), refresh_interval_bars=0))

        with self.assertRaises(ValueError):
            strategy.on_bar(_bar(), history=[], open_positions={})

    @patch("core.news.google_news_feed.urlopen")
    def test_falls_back_to_a_real_google_news_feed_when_none_injected(self, mock_urlopen) -> None:
        # No news_feed param set -> on_bar must build a real GoogleNewsFeed
        # itself (see test_google_news_feed.py for that class's own
        # parsing coverage; urlopen is patched here only so this stays
        # network-free).
        response = MagicMock()
        response.read.return_value = b"<rss><channel></channel></rss>"
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        mock_urlopen.return_value = response

        strategy = NewsSentimentSignalStrategy(_context())
        signals = strategy.on_bar(_bar(), history=[], open_positions={})

        self.assertEqual(signals, [])
        mock_urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()

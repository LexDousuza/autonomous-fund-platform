"""
Unit tests for core.market_data.candidate_prescreen.

Both the AngelOneMarketDataTransport and the news feed are fake doubles -
never touches a real socket. Mirrors tests/unit/test_news_sentiment_signal_strategy.py's
FakeNewsFeed pattern.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_candidate_prescreen.py -v
"""
from __future__ import annotations

import unittest

from core.market_data.candidate_prescreen import PrescreenCriteria, prescreen_candidates
from core.market_data.universe_models import Instrument
from core.news.google_news_feed import NewsFeedError, NewsItem


class FakeTransport:
    def __init__(self, ltp_by_key: dict[tuple[str, str], float]) -> None:
        self._ltp_by_key = ltp_by_key
        self.batch_calls: list[list[tuple[str, str]]] = []

    def get_ltp_batch(self, symbols, sleep_fn=None):
        self.batch_calls.append(list(symbols))
        return {key: value for key, value in self._ltp_by_key.items() if key in symbols}


class FakeNewsFeed:
    def __init__(self, items_by_query: dict[str, list[NewsItem]] | None = None, error_for: set[str] | None = None) -> None:
        self.items_by_query = items_by_query or {}
        self.error_for = error_for or set()
        self.calls: list[tuple[str, int]] = []

    def fetch(self, query: str, max_items: int = 10) -> list[NewsItem]:
        self.calls.append((query, max_items))
        if query in self.error_for:
            raise NewsFeedError(f"boom: {query}")
        return self.items_by_query.get(query, [])


def _instrument(symbol: str, exchange: str = "NSE") -> Instrument:
    return Instrument(symbol=symbol, exchange=exchange)


def _bullish(symbol: str) -> NewsItem:
    return NewsItem(query=symbol, headline="Stock surges on record profit growth", link="", source=None, published_at=None)


def _neutral(symbol: str) -> NewsItem:
    return NewsItem(query=symbol, headline="Company holds AGM next week", link="", source=None, published_at=None)


class PrescreenCandidatesTests(unittest.TestCase):
    def test_unaffordable_symbol_excluded_before_any_news_check(self) -> None:
        instruments = [_instrument("CHEAP"), _instrument("EXPENSIVE")]
        transport = FakeTransport({("CHEAP", "NSE"): 100.0, ("EXPENSIVE", "NSE"): 50000.0})
        news_feed = FakeNewsFeed({"CHEAP NSE share price": [_bullish("CHEAP")]})
        criteria = PrescreenCriteria(max_capital_per_trade=2500.0)

        result = prescreen_candidates(instruments, transport, criteria, news_feed=news_feed, sleep_fn=lambda _s: None)

        self.assertEqual([i.symbol for i in result.survivors], ["CHEAP"])
        self.assertEqual([i.symbol for i in result.excluded_unaffordable], ["EXPENSIVE"])
        # EXPENSIVE was never even news-checked - excluded before that stage.
        self.assertEqual(news_feed.calls, [("CHEAP NSE share price", 5)])

    def test_symbol_with_no_price_data_excluded_separately_from_unaffordable(self) -> None:
        instruments = [_instrument("NOPRICE")]
        transport = FakeTransport({})
        criteria = PrescreenCriteria(max_capital_per_trade=2500.0)

        result = prescreen_candidates(instruments, transport, criteria, news_feed=FakeNewsFeed(), sleep_fn=lambda _s: None)

        self.assertEqual(result.survivors, [])
        self.assertEqual(result.excluded_unaffordable, [])
        self.assertEqual([i.symbol for i in result.excluded_price_unavailable], ["NOPRICE"])

    def test_affordable_but_no_bullish_news_is_excluded(self) -> None:
        instruments = [_instrument("QUIET")]
        transport = FakeTransport({("QUIET", "NSE"): 100.0})
        news_feed = FakeNewsFeed({"QUIET NSE share price": [_neutral("QUIET")]})
        criteria = PrescreenCriteria(max_capital_per_trade=2500.0)

        result = prescreen_candidates(instruments, transport, criteria, news_feed=news_feed, sleep_fn=lambda _s: None)

        self.assertEqual(result.survivors, [])
        self.assertEqual([i.symbol for i in result.excluded_no_news], ["QUIET"])

    def test_news_feed_error_treated_as_no_news_not_a_crash(self) -> None:
        instruments = [_instrument("FLAKY")]
        transport = FakeTransport({("FLAKY", "NSE"): 100.0})
        news_feed = FakeNewsFeed(error_for={"FLAKY NSE share price"})
        criteria = PrescreenCriteria(max_capital_per_trade=2500.0)

        result = prescreen_candidates(instruments, transport, criteria, news_feed=news_feed, sleep_fn=lambda _s: None)

        self.assertEqual(result.survivors, [])
        self.assertEqual([i.symbol for i in result.excluded_no_news], ["FLAKY"])

    def test_news_checks_are_paced_between_calls_not_before_the_first(self) -> None:
        instruments = [_instrument("A"), _instrument("B"), _instrument("C")]
        transport = FakeTransport({("A", "NSE"): 1.0, ("B", "NSE"): 1.0, ("C", "NSE"): 1.0})
        news_feed = FakeNewsFeed({})
        criteria = PrescreenCriteria(max_capital_per_trade=2500.0, news_pacing_seconds=1.0)
        sleeps: list[float] = []

        prescreen_candidates(instruments, transport, criteria, news_feed=news_feed, sleep_fn=sleeps.append)

        self.assertEqual(sleeps, [1.0, 1.0])  # 2 sleeps between 3 sequential checks

    def test_source_order_is_preserved_in_every_output_list(self) -> None:
        instruments = [_instrument("Z"), _instrument("A"), _instrument("M")]
        transport = FakeTransport({("Z", "NSE"): 1.0, ("A", "NSE"): 1.0, ("M", "NSE"): 1.0})
        news_feed = FakeNewsFeed(
            {
                "Z NSE share price": [_bullish("Z")],
                "A NSE share price": [_bullish("A")],
                "M NSE share price": [_bullish("M")],
            }
        )
        criteria = PrescreenCriteria(max_capital_per_trade=2500.0)

        result = prescreen_candidates(instruments, transport, criteria, news_feed=news_feed, sleep_fn=lambda _s: None)

        self.assertEqual([i.symbol for i in result.survivors], ["Z", "A", "M"])

    def test_detail_by_symbol_carries_the_actual_numbers_behind_each_decision(self) -> None:
        instruments = [_instrument("CHEAP"), _instrument("EXPENSIVE"), _instrument("QUIET")]
        transport = FakeTransport(
            {("CHEAP", "NSE"): 100.0, ("EXPENSIVE", "NSE"): 50000.0, ("QUIET", "NSE"): 200.0}
        )
        news_feed = FakeNewsFeed(
            {
                "CHEAP NSE share price": [_bullish("CHEAP")],
                "QUIET NSE share price": [_neutral("QUIET")],
            }
        )
        criteria = PrescreenCriteria(max_capital_per_trade=2500.0)

        result = prescreen_candidates(instruments, transport, criteria, news_feed=news_feed, sleep_fn=lambda _s: None)

        cheap = result.detail_by_symbol["CHEAP"]
        self.assertEqual(cheap.ltp, 100.0)
        self.assertEqual(cheap.news_score, 4)  # surges + record + profit + growth
        self.assertEqual(cheap.headlines, ["Stock surges on record profit growth"])

        expensive = result.detail_by_symbol["EXPENSIVE"]
        self.assertEqual(expensive.ltp, 50000.0)
        self.assertIsNone(expensive.news_score)  # never news-checked - cut before that stage
        self.assertEqual(expensive.headlines, [])

        quiet = result.detail_by_symbol["QUIET"]
        self.assertEqual(quiet.ltp, 200.0)
        self.assertEqual(quiet.news_score, 0)
        self.assertEqual(quiet.headlines, ["Company holds AGM next week"])

    def test_price_unavailable_detail_has_none_ltp(self) -> None:
        instruments = [_instrument("NOPRICE")]
        transport = FakeTransport({})
        criteria = PrescreenCriteria(max_capital_per_trade=2500.0)

        result = prescreen_candidates(instruments, transport, criteria, news_feed=FakeNewsFeed(), sleep_fn=lambda _s: None)

        self.assertIsNone(result.detail_by_symbol["NOPRICE"].ltp)


if __name__ == "__main__":
    unittest.main()

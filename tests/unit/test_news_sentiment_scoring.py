"""
Unit tests for core.news.sentiment.score_headlines.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_news_sentiment_scoring.py -v
"""
from __future__ import annotations

import unittest

from core.news.google_news_feed import NewsItem
from core.news.sentiment import score_headlines


def _item(headline: str) -> NewsItem:
    return NewsItem(query="X", headline=headline, link="", source=None, published_at=None)


class ScoreHeadlinesTests(unittest.TestCase):
    def test_empty_list_scores_zero(self) -> None:
        self.assertEqual(score_headlines([]), 0)

    def test_positive_headline_scores_positive(self) -> None:
        score = score_headlines([_item("Stock surges after record profit growth")])
        self.assertGreater(score, 0)

    def test_negative_headline_scores_negative(self) -> None:
        score = score_headlines([_item("Company shares crash after fraud probe")])
        self.assertLess(score, 0)

    def test_neutral_headline_scores_zero(self) -> None:
        score = score_headlines([_item("Company to hold annual general meeting next week")])
        self.assertEqual(score, 0)

    def test_scores_sum_across_multiple_headlines(self) -> None:
        combined = score_headlines(
            [
                _item("Stock surges after record profit growth"),
                _item("Company shares crash after fraud probe"),
            ]
        )
        individual_sum = score_headlines(
            [_item("Stock surges after record profit growth")]
        ) + score_headlines([_item("Company shares crash after fraud probe")])
        self.assertEqual(combined, individual_sum)

    def test_case_insensitive_matching(self) -> None:
        lower = score_headlines([_item("stock surges on strong growth")])
        upper = score_headlines([_item("STOCK SURGES ON STRONG GROWTH")])
        self.assertEqual(lower, upper)
        self.assertGreater(lower, 0)

    def test_each_keyword_counted_once_per_occurrence_word(self) -> None:
        # "surge" and "growth" are both positive keywords -> score 2.
        score = score_headlines([_item("Surge in growth reported")])
        self.assertEqual(score, 2)


if __name__ == "__main__":
    unittest.main()

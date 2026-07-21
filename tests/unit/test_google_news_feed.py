"""
Unit tests for core.news.google_news_feed.

Never touches the real network: urlopen is patched at the module import
site with a canned RSS response.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_google_news_feed.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from core.news.google_news_feed import GoogleNewsFeed, NewsFeedError

_SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>"RELIANCE" - Google News</title>
    <item>
      <title>Reliance shares surge after strong profit growth</title>
      <link>https://news.google.com/articles/abc</link>
      <pubDate>Mon, 05 Jan 2026 10:00:00 GMT</pubDate>
      <source url="https://example.com">Example News</source>
    </item>
    <item>
      <title>Reliance faces probe over accounting practices</title>
      <link>https://news.google.com/articles/def</link>
      <pubDate>Sun, 04 Jan 2026 08:30:00 GMT</pubDate>
      <source url="https://example2.com">Example Wire</source>
    </item>
  </channel>
</rss>
"""


def _mock_urlopen_response(body: bytes) -> MagicMock:
    response = MagicMock()
    response.read.return_value = body
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


class GoogleNewsFeedFetchTests(unittest.TestCase):
    @patch("core.news.google_news_feed.urlopen")
    def test_parses_headlines_link_source_and_pub_date(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen_response(_SAMPLE_RSS)

        items = GoogleNewsFeed().fetch("RELIANCE")

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].headline, "Reliance shares surge after strong profit growth")
        self.assertEqual(items[0].link, "https://news.google.com/articles/abc")
        self.assertEqual(items[0].source, "Example News")
        self.assertEqual(
            items[0].published_at, datetime(2026, 1, 5, 10, 0, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(items[0].query, "RELIANCE")

    @patch("core.news.google_news_feed.urlopen")
    def test_max_items_truncates(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen_response(_SAMPLE_RSS)

        items = GoogleNewsFeed().fetch("RELIANCE", max_items=1)

        self.assertEqual(len(items), 1)

    @patch("core.news.google_news_feed.urlopen")
    def test_query_is_url_encoded(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen_response(b"<rss><channel></channel></rss>")

        GoogleNewsFeed().fetch("RELIANCE NSE stock")

        request = mock_urlopen.call_args[0][0]
        self.assertIn("RELIANCE+NSE+stock", request.full_url)

    @patch("core.news.google_news_feed.urlopen")
    def test_empty_feed_returns_empty_list(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen_response(b"<rss><channel></channel></rss>")

        self.assertEqual(GoogleNewsFeed().fetch("NOTHING"), [])

    @patch("core.news.google_news_feed.urlopen")
    def test_network_failure_raises_news_feed_error(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = URLError("connection refused")

        with self.assertRaises(NewsFeedError):
            GoogleNewsFeed().fetch("RELIANCE")

    @patch("core.news.google_news_feed.urlopen")
    def test_malformed_xml_raises_news_feed_error(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen_response(b"not xml at all <<<")

        with self.assertRaises(NewsFeedError):
            GoogleNewsFeed().fetch("RELIANCE")


if __name__ == "__main__":
    unittest.main()

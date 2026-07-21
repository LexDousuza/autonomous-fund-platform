"""
Free, no-API-key news fetching via Google News' public RSS search feed.

No signup, no key, no rate-limit budget to manage - the tradeoff for that
is an unofficial endpoint (Google could change or throttle it without
notice) and no SLA. Good enough for a V1 news signal; if this becomes
load-bearing for live trading decisions, a paid news API with a real
SLA/retry contract (matching core/market_data/groww_transport.py's
CircuitBreaker/RetryPolicy pattern) is the natural upgrade.

Deliberately not modeled after GrowwMarketDataTransport's retry/circuit-
breaker machinery: a single unauthenticated public feed, called at most
once per symbol per cooldown window (see core/strategy/news_sentiment_signal.py),
doesn't carry the same "many calls per session" failure-amplification risk
that justified that machinery for the broker/market-data path.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.error import URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

_RSS_URL_TEMPLATE = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
_USER_AGENT = "Mozilla/5.0 (compatible; AQTPNewsFeed/1.0)"
_DEFAULT_TIMEOUT_SECONDS = 10.0


class NewsFeedError(RuntimeError):
    """The news feed request failed or returned something this parser couldn't understand."""


@dataclass(frozen=True, slots=True)
class NewsItem:
    query: str
    headline: str
    link: str
    source: str | None
    published_at: datetime | None


class GoogleNewsFeed:
    """
    Fetches recent headlines for a free-text query via Google News RSS.

    Stateless and side-effect-free beyond the HTTP call itself - no
    caching, no dedup across calls. Callers that need a per-symbol
    refresh cooldown (so every bar doesn't trigger a fresh request)
    implement that themselves - see NewsSentimentSignalStrategy.
    """

    def __init__(self, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def fetch(self, query: str, max_items: int = 10) -> list[NewsItem]:
        url = _RSS_URL_TEMPLATE.format(query=quote_plus(query))
        request = Request(url, headers={"User-Agent": _USER_AGENT})

        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                body = response.read()
        except (URLError, TimeoutError, OSError) as exc:
            raise NewsFeedError(f"Failed to fetch news for query '{query}': {exc}") from exc

        try:
            root = ET.fromstring(body)  # noqa: S314 - trusted, well-formed RSS from Google News
        except ET.ParseError as exc:
            raise NewsFeedError(f"Malformed RSS response for query '{query}': {exc}") from exc

        items: list[NewsItem] = []
        for item_element in root.findall(".//item")[:max_items]:
            headline = (item_element.findtext("title") or "").strip()
            if not headline:
                continue

            items.append(
                NewsItem(
                    query=query,
                    headline=headline,
                    link=(item_element.findtext("link") or "").strip(),
                    source=_clean_source(item_element.findtext("source")),
                    published_at=_parse_pub_date(item_element.findtext("pubDate")),
                )
            )

        return items


def _clean_source(raw: str | None) -> str | None:
    stripped = (raw or "").strip()
    return stripped or None


def _parse_pub_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None

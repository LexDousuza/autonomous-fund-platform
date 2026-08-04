"""
Pre-screens a large candidate universe down to a small batch worth a full
historical fetch, using two checks that don't need per-symbol historical
data:

- Affordability: one batched LTP call (``AngelOneMarketDataTransport.
  get_ltp_batch``, 50 symbols/request) instead of a full 10-day historical
  fetch per symbol just to read the latest close - ``bucket_selector.py``'s
  affordability check only ever needed that one number.
- News relevance: real coverage today (``core/news/google_news_feed.py`` +
  ``core/news/sentiment.py``), one request per symbol that survives the
  affordability cut, paced to stay polite to Google's free/unofficial RSS
  endpoint (no documented rate limit to target, so this picks a
  conservative default rather than guessing an aggressive one).

# ponytail: sequential news checks, one request per second by default - no
# bulk news API exists, so this is the ceiling on how fast this stage can
# go. Upgrade path: a paid news API with bulk headline fetch, if the
# affordability cut alone doesn't shrink the batch enough to keep this
# stage fast.

This is a deliberate strategy-shaping decision, not just a performance
optimization: only symbols that survive both cuts ever reach the
historical fetch + ``CapitalRiskBucketSelector``, so any strategy trading
today's session only ever sees symbols with real news coverage today.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from core.market_data.angelone_transport import AngelOneMarketDataTransport
from core.market_data.universe_models import Instrument
from core.news.google_news_feed import GoogleNewsFeed, NewsFeedError
from core.news.sentiment import score_headlines

_DEFAULT_MIN_NEWS_SCORE = 1
_DEFAULT_MAX_HEADLINES = 5
_DEFAULT_QUERY_TEMPLATE = "{symbol} NSE share price"
_DEFAULT_NEWS_PACING_SECONDS = 1.0


@dataclass(slots=True)
class PrescreenCriteria:
    max_capital_per_trade: float
    min_news_score: int = _DEFAULT_MIN_NEWS_SCORE
    max_headlines: int = _DEFAULT_MAX_HEADLINES
    news_query_template: str = _DEFAULT_QUERY_TEMPLATE
    news_pacing_seconds: float = _DEFAULT_NEWS_PACING_SECONDS


@dataclass(slots=True)
class PrescreenDetail:
    """
    The actual numbers a candidate's prescreen decision was made from -
    keyed by symbol in ``PrescreenResult.detail_by_symbol`` so any caller
    (a banner, a dashboard) can answer "why" for a specific symbol
    without re-deriving it.

    ``ltp`` is ``None`` for ``excluded_price_unavailable`` (there was
    nothing to read). ``news_score``/``headlines`` are unset for
    ``excluded_unaffordable``/``excluded_price_unavailable`` - the news
    check never ran for those, since they were already cut before it.
    """

    symbol: str
    exchange: str
    ltp: float | None = None
    news_score: int | None = None
    headlines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PrescreenResult:
    survivors: list[Instrument] = field(default_factory=list)
    excluded_price_unavailable: list[Instrument] = field(default_factory=list)
    excluded_unaffordable: list[Instrument] = field(default_factory=list)
    excluded_no_news: list[Instrument] = field(default_factory=list)
    detail_by_symbol: dict[str, PrescreenDetail] = field(default_factory=dict)


def prescreen_candidates(
    instruments: list[Instrument],
    transport: AngelOneMarketDataTransport,
    criteria: PrescreenCriteria,
    news_feed: GoogleNewsFeed | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> PrescreenResult:
    """
    Narrow ``instruments`` down to those affordable at
    ``criteria.max_capital_per_trade`` (by batched LTP) AND showing
    net-bullish-or-neutral-or-better news coverage today (by
    ``score_headlines(...) >= criteria.min_news_score``) - both cuts, not
    either.

    Deterministic in ordering (source ``instruments`` order preserved in
    every output list), not in *result* - news coverage genuinely changes
    day to day, so re-running this later for the same universe can
    legitimately return a different survivor list.
    """
    news_feed = news_feed or GoogleNewsFeed()

    ltp_by_key = transport.get_ltp_batch([(i.symbol, i.exchange) for i in instruments])

    affordable: list[Instrument] = []
    result = PrescreenResult()
    for instrument in instruments:
        ltp = ltp_by_key.get((instrument.symbol, instrument.exchange))
        result.detail_by_symbol[instrument.symbol] = PrescreenDetail(
            symbol=instrument.symbol, exchange=instrument.exchange, ltp=ltp
        )
        if ltp is None:
            result.excluded_price_unavailable.append(instrument)
        elif ltp > criteria.max_capital_per_trade:
            result.excluded_unaffordable.append(instrument)
        else:
            affordable.append(instrument)

    for index, instrument in enumerate(affordable):
        if index > 0:
            sleep_fn(criteria.news_pacing_seconds)

        query = criteria.news_query_template.format(symbol=instrument.symbol)
        try:
            items = news_feed.fetch(query, max_items=criteria.max_headlines)
            score = score_headlines(items)
            headlines = [item.headline for item in items]
        except NewsFeedError:
            score = 0  # best-effort signal - a feed hiccup excludes, doesn't crash the session
            headlines = []

        detail = result.detail_by_symbol[instrument.symbol]
        detail.news_score = score
        detail.headlines = headlines

        if score >= criteria.min_news_score:
            result.survivors.append(instrument)
        else:
            result.excluded_no_news.append(instrument)

    return result

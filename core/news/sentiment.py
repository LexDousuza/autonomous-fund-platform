"""
Naive keyword-polarity headline scoring.

# ponytail: bag-of-words keyword counting, not real NLP - no negation
# handling ("not a fraud" scores as negative), no context, no
# language model. Upgrade path: a real sentiment classifier or an LLM
# call scoring each headline, swapped in behind score_headlines'
# same (list[NewsItem]) -> float signature so
# core/strategy/news_sentiment_signal.py doesn't need to change.

Deliberately a pure function of already-fetched headlines - no network
I/O here, matching core/market_data/bucket_selector.py's same
"pure function of (data, criteria)" shape, so this is testable without
mocking anything.
"""

from __future__ import annotations

import re

from core.news.google_news_feed import NewsItem

_POSITIVE_WORDS = frozenset(
    {
        "surge", "surges", "surged", "rally", "rallies", "rallied", "beat", "beats",
        "upgrade", "upgraded", "upgrades", "profit", "profits", "growth", "record",
        "strong", "bullish", "gain", "gains", "jump", "jumps", "jumped", "soar",
        "soars", "soared", "outperform", "buy", "boost", "boosts", "boosted",
        "rise", "rises", "rising", "high", "wins", "win", "expansion", "positive",
    }
)
_NEGATIVE_WORDS = frozenset(
    {
        "plunge", "plunges", "plunged", "crash", "crashes", "crashed", "miss",
        "misses", "missed", "downgrade", "downgraded", "downgrades", "loss",
        "losses", "fraud", "probe", "investigation", "weak", "bearish", "fall",
        "falls", "fell", "falling", "decline", "declines", "declined", "lawsuit",
        "scandal", "sell", "selloff", "slump", "slumps", "slumped", "cut", "cuts",
        "layoffs", "default", "downturn", "negative", "warning", "warns",
    }
)

_WORD_PATTERN = re.compile(r"[a-zA-Z]+")


def _headline_score(headline: str) -> int:
    words = {match.group(0).lower() for match in _WORD_PATTERN.finditer(headline)}
    return len(words & _POSITIVE_WORDS) - len(words & _NEGATIVE_WORDS)


def score_headlines(items: list[NewsItem]) -> int:
    """
    Sum of each headline's (positive keyword count - negative keyword
    count). Positive = net-bullish coverage, negative = net-bearish,
    zero = no signal either way (including an empty ``items`` list).
    """
    return sum(_headline_score(item.headline) for item in items)

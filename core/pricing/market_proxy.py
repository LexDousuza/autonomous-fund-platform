"""
Equal-weighted market-index proxy, built from a fixed basket of symbols'
own bars - a stand-in for a real index feed when one isn't available at
the same intraday granularity as the traded universe (see
``core/strategy/ornstein_uhlenbeck_residual.py``, which needs a market
factor to compute each stock's residual return - the real NIFTY spot
series in ``data/processed/nse_index_options_iv_history.csv`` is
daily-only, not intraday, so it can't serve this purpose).
"""

from __future__ import annotations

from datetime import datetime
from math import log

from core.strategy.base import MarketBar


class EqualWeightedMarketProxy:
    """
    ``log_return_at(timestamp, exclude_symbol)`` returns the mean
    per-symbol log-return into ``timestamp`` across every basket symbol
    that has both this and its immediately preceding bar - the average of
    each constituent's OWN return, not the return of the average price
    level (the standard convention for an equal-weight index return, and
    the one that doesn't drift with constituent price levels the way
    "return of the average price" would).

    ``exclude_symbol`` drops one symbol from its own basket average - use
    this when computing a symbol's residual against "the market", so a
    small (e.g. 10-name) proxy doesn't include the very stock being
    measured. A real NIFTY-sized index wouldn't need this (one name is a
    rounding error in 50); a small proxy meaningfully would.
    """

    def __init__(self, bars_by_symbol: dict[str, list[MarketBar]]) -> None:
        self._returns_by_symbol_and_timestamp: dict[str, dict[datetime, float]] = {}
        for symbol, bars in bars_by_symbol.items():
            sorted_bars = sorted(bars, key=lambda b: b.timestamp)
            per_timestamp: dict[datetime, float] = {}
            for previous, current in zip(sorted_bars, sorted_bars[1:]):
                if previous.close > 0 and current.close > 0:
                    per_timestamp[current.timestamp] = log(current.close / previous.close)
            self._returns_by_symbol_and_timestamp[symbol] = per_timestamp

    def log_return_at(self, timestamp: datetime, exclude_symbol: str | None = None) -> float | None:
        """``None`` if no basket symbol (other than ``exclude_symbol``) has a return at exactly this timestamp."""
        contributing = [
            returns[timestamp]
            for symbol, returns in self._returns_by_symbol_and_timestamp.items()
            if symbol != exclude_symbol and timestamp in returns
        ]
        if not contributing:
            return None
        return sum(contributing) / len(contributing)

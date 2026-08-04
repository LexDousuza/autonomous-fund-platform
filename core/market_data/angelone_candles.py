"""
Shared parsing for Angel One (SmartAPI) ``getCandleData`` responses -
used by both ``angelone_historical_feed.py`` (batch, day-granularity by
default) and ``angelone_intraday_feed.py`` (polled, minute-granularity),
so the verified candle shape and IST-offset normalization only live in
one place.
"""

from __future__ import annotations

from datetime import datetime

from core.market_data.errors import MarketDataTransportError
from core.strategy.base import MarketBar


def parse_candles(symbol: str, exchange: str, candles: list[list]) -> list[MarketBar]:
    """
    Parse Angel One's raw positional candle arrays into ``MarketBar``s.
    Verified shape: ``[timestamp_iso8601, open, high, low, close, volume]``.
    """
    parsed: list[MarketBar] = []
    for candle in candles:
        try:
            timestamp_raw, open_, high, low, close, volume = candle
        except (ValueError, TypeError) as exc:
            raise MarketDataTransportError(
                f"Malformed Angel One candle for '{symbol}': expected 6 fields, got {candle!r}"
            ) from exc

        parsed.append(
            MarketBar(
                symbol=symbol,
                exchange=exchange,
                timestamp=normalize_timestamp(timestamp_raw),
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume),
            )
        )

    return parsed


def normalize_timestamp(value: str) -> datetime:
    """
    Angel One returns an ISO8601 string with a ``+05:30`` (IST) offset
    (e.g. ``"2023-09-06T11:15:00+05:30"``) - normalized to a naive
    ``datetime``, matching this codebase's naive-datetime convention.
    """
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed

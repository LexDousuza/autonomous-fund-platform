"""
Small, pure technical indicators computed from ``MarketBar`` history -
extra data points strategies and the exit engine can reason from beyond
raw OHLCV. Each function takes whatever bar list it's given and returns
``None`` when there isn't enough history yet, rather than raising - a
strategy/exit check on bar 3 of a session with no indicator yet is a
normal, expected state, not an error.
"""
from __future__ import annotations

from core.strategy.base import MarketBar


def simple_moving_average(bars: list[MarketBar], n: int) -> float | None:
    """Mean close of the last ``n`` bars, or ``None`` if fewer than ``n`` are available."""
    if n <= 0:
        raise ValueError("n must be >= 1")
    if len(bars) < n:
        return None
    window = bars[-n:]
    return sum(bar.close for bar in window) / n


def vwap(bars: list[MarketBar]) -> float | None:
    """
    Volume-weighted average price over ``bars``: sum(typical_price *
    volume) / sum(volume), typical_price = (high + low + close) / 3.
    ``None`` if ``bars`` is empty or every bar has zero volume (e.g. the
    LTP-only Angel One live feed - see ``angelone_live_feed.py`` - where
    this would be meaningless).
    """
    total_volume = sum(bar.volume for bar in bars)
    if total_volume <= 0:
        return None
    weighted_sum = sum(((bar.high + bar.low + bar.close) / 3) * bar.volume for bar in bars)
    return weighted_sum / total_volume


def average_true_range(bars: list[MarketBar], n: int) -> float | None:
    """
    Average true range over the last ``n`` bars: mean of
    ``max(high-low, |high-prev_close|, |low-prev_close|)``. ``None`` if
    fewer than ``n + 1`` bars are available (the first true-range value
    needs a previous close). A per-symbol-volatility read, unlike a flat
    stop/target percentage - see ``core.portfolio.exit_models.ExitPolicyConfig.atr_multiplier``.
    """
    if n <= 0:
        raise ValueError("n must be >= 1")
    if len(bars) < n + 1:
        return None
    window = bars[-(n + 1) :]
    true_ranges = []
    for previous, current in zip(window, window[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(true_ranges) / len(true_ranges)

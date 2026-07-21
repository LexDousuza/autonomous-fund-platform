"""
Deterministic synthetic historical market data generator for backtesting.

IMPORTANT: this generates *synthetic* price action - a seeded random walk
with realistic-ish intraday volatility and a modest, well-known
volume/volatility correlation (bigger moves trade more volume - a
standard, unbiased stylized fact of real markets, not a
strategy-favoring injection). It is not real historical prices for any
real instrument. It exists so the backtest engine
(``core/backtest/day_runner.py``) can be exercised and a strategy's
mechanics evaluated end-to-end without a real Groww historical-data
connection.

A backtest run against this data answers "does the system and the
strategy's logic behave correctly and sensibly across many days and
symbols" - it is not a prediction of real market profitability, and must
never be reported as one. Once real Groww historical data is available
(see ``core/market_data/groww_historical_feed.py``), the same backtest
engine should be pointed at that instead - nothing about the day-runner
or report is specific to synthetic data.

Deterministic: the same ``(symbols, start_date, num_days, seed)`` always
produces the same bars, matching this platform's "identical inputs ->
identical outputs" principle (see CLAUDE.md).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from core.strategy.base import MarketBar

_MARKET_OPEN = time(9, 15)
_MARKET_CLOSE = time(15, 30)


@dataclass(frozen=True, slots=True)
class SyntheticSymbolConfig:
    """
    ``daily_volatility_pct`` is a rough day-to-day close-to-close std dev
    figure (e.g. ``1.2`` for a typical liquid NSE large-cap) - scaled
    down per bar internally, not a literal per-bar percentage.

    ``momentum_strength`` (0.0-1.0, default 0.0): fraction of each bar's
    return that blends in the *previous* bar's return, giving the series
    modest short-term autocorrelation instead of being pure i.i.d. noise.
    0.0 reproduces a pure random walk. This models a real, if modest and
    debated, empirical property of short-horizon liquid-market returns -
    it is a documented model choice, not a strategy-favoring rig: the
    same parameter would help or hurt a momentum strategy and a
    mean-reversion strategy in exactly the mirrored way. Resets to 0 at
    the start of every trading day (intraday momentum only, matching
    this platform's intraday-only scope).
    """

    symbol: str
    exchange: str
    starting_price: float
    daily_volatility_pct: float = 1.2
    base_volume: float = 50000.0
    momentum_strength: float = 0.0


def _trading_days(start_date: date, num_days: int) -> list[date]:
    """``num_days`` consecutive weekdays starting from ``start_date`` (skips Sat/Sun; no holiday calendar)."""
    if num_days < 1:
        raise ValueError("num_days must be >= 1")

    days: list[date] = []
    current = start_date
    while len(days) < num_days:
        if current.weekday() < 5:  # Monday-Friday
            days.append(current)
        current += timedelta(days=1)
    return days


def _bar_timestamps(day: date, interval_minutes: int) -> list[datetime]:
    timestamps = []
    current = datetime.combine(day, _MARKET_OPEN)
    end = datetime.combine(day, _MARKET_CLOSE)
    while current <= end:
        timestamps.append(current)
        current += timedelta(minutes=interval_minutes)
    return timestamps


def generate_synthetic_bars(
    symbols: list[SyntheticSymbolConfig],
    start_date: date,
    num_days: int,
    interval_minutes: int = 5,
    seed: int = 42,
) -> list[MarketBar]:
    """
    Generate a deterministic, chronologically-interleaved synthetic bar
    stream for every symbol in ``symbols``, across ``num_days`` trading
    days starting ``start_date``.

    Each symbol's price carries over from the previous day's close, with
    a small overnight gap - more realistic (and less repetitive) than
    resetting to ``starting_price`` every day. Per-bar return is drawn
    from a normal distribution sized off ``daily_volatility_pct`` (scaled
    down by the number of bars in a trading day, since variance scales
    with time), optionally blended with the previous bar's return per
    ``SyntheticSymbolConfig.momentum_strength``; intrabar high/low add a
    small amount of noise around open/close.
    """
    rng = random.Random(seed)
    days = _trading_days(start_date, num_days)
    bars_per_day = len(_bar_timestamps(days[0], interval_minutes))

    bars: list[MarketBar] = []
    for symbol_cfg in symbols:
        price = symbol_cfg.starting_price
        per_bar_sigma = (symbol_cfg.daily_volatility_pct / 100) / (bars_per_day**0.5)

        for day in days:
            gap_return = rng.gauss(0, per_bar_sigma * 2)
            price = max(price * (1 + gap_return), 0.05)
            previous_pct_return = 0.0  # momentum resets every day - intraday only

            for timestamp in _bar_timestamps(day, interval_minutes):
                open_price = price
                noise_return = rng.gauss(0, per_bar_sigma)
                pct_return = (
                    (1 - symbol_cfg.momentum_strength) * noise_return
                    + symbol_cfg.momentum_strength * previous_pct_return
                )
                previous_pct_return = pct_return
                close_price = max(open_price * (1 + pct_return), 0.05)

                intrabar_noise = abs(rng.gauss(0, per_bar_sigma)) * open_price
                high = max(open_price, close_price) + intrabar_noise
                low = max(min(open_price, close_price) - intrabar_noise, 0.01)

                volume = symbol_cfg.base_volume * (1 + 3 * abs(pct_return)) * rng.uniform(0.7, 1.3)

                bars.append(
                    MarketBar(
                        symbol=symbol_cfg.symbol,
                        exchange=symbol_cfg.exchange,
                        timestamp=timestamp,
                        open=round(open_price, 2),
                        high=round(high, 2),
                        low=round(low, 2),
                        close=round(close_price, 2),
                        volume=round(volume, 0),
                    )
                )
                price = close_price

    bars.sort(key=lambda bar: (bar.timestamp, bar.symbol))
    return bars

"""
Dynamic NSE/BSE cash-equity universe screening.

Two independent passes, mirroring ``bucket_selector.py``'s shape (pure
functions of (data, criteria), deterministic, excluded-with-reason):

- ``screen_instrument_master``: filters Groww's raw instrument-master
  rows (see ``GrowwMarketDataTransport.list_all_instruments``) down to
  real, currently-tradable CASH/EQ instruments on the configured
  exchange(s). Identifiers only - no liquidity/trend judgment yet.
- ``screen_by_liquidity_and_trend``: a second pass over that candidate
  list, given each candidate's recent price history, applying a
  liquidity (mean daily volume) floor and a simple uptrend filter (last
  close above its own N-bar simple moving average). Deliberately simple
  V1 proxies, not a fundamentals-driven screen - Groww's instrument
  master carries no market-cap/fundamentals fields at all.

Neither pass touches ``config/instruments/*.yaml`` directly - see
``scripts/refresh_universe.py`` for turning a screening result into a
written universe file. This keeps ``config/`` declarative per
``CLAUDE.md``: screening runs offline, not in the live/paper/backtest
trading hot path (none of ``apps/paper_trader``, ``apps/live_trader``, or
``apps/backtester/run_backtest.py`` import this module).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from core.market_data.universe_models import Instrument
from core.strategy.base import MarketBar

_DEFAULT_SEGMENT = "CASH"
_DEFAULT_SERIES = "EQ"


@dataclass(slots=True)
class InstrumentMasterScreeningCriteria:
    """Static filters applied to Groww's raw instrument-master rows."""

    exchanges: tuple[str, ...] = ("NSE",)
    segment: str = _DEFAULT_SEGMENT
    series: str = _DEFAULT_SERIES
    require_buy_allowed: bool = True
    require_sell_allowed: bool = True


def _is_truthy(value: object) -> bool:
    """
    Groww's instrument CSV loads every column as strings (see
    ``GrowwAPI.get_all_instruments``) - ``"True"``/``"False"`` text, not
    real booleans.
    """
    return str(value).strip().lower() in {"true", "1", "yes"}


def screen_instrument_master(
    records: list[dict[str, object]],
    criteria: InstrumentMasterScreeningCriteria | None = None,
) -> list[Instrument]:
    """
    Filter Groww's raw instrument-master rows down to real,
    currently-tradable CASH/EQ ``Instrument``s on ``criteria.exchanges``.

    Deterministic: returns matching rows in their source order.
    """
    criteria = criteria or InstrumentMasterScreeningCriteria()
    exchanges = set(criteria.exchanges)

    selected: list[Instrument] = []
    for record in records:
        if record.get("exchange") not in exchanges:
            continue
        if record.get("segment") != criteria.segment:
            continue
        if record.get("series") != criteria.series:
            continue
        if criteria.require_buy_allowed and not _is_truthy(record.get("buy_allowed")):
            continue
        if criteria.require_sell_allowed and not _is_truthy(record.get("sell_allowed")):
            continue

        symbol = record.get("trading_symbol")
        exchange = record.get("exchange")
        if not symbol or not exchange:
            continue

        selected.append(
            Instrument(
                symbol=str(symbol),
                exchange=str(exchange),
                segment=criteria.segment,
                metadata={"name": record.get("name"), "isin": record.get("isin")},
            )
        )

    return selected


@dataclass(slots=True)
class LiquidityTrendScreeningCriteria:
    """
    ``min_avg_daily_volume`` and ``trend_lookback_bars`` are read against
    whatever price history is supplied - daily candles for a "daily
    volume"/"daily trend" reading in the normal case (see
    ``scripts/refresh_universe.py``), but this function itself doesn't
    care what bar interval the history uses.
    """

    min_avg_daily_volume: float
    trend_lookback_bars: int = 10
    max_symbols: int | None = None


@dataclass(slots=True)
class ScreeningExclusion:
    symbol: str
    exchange: str
    reason: str


@dataclass(slots=True)
class LiquidityTrendScreeningResult:
    selected: list[Instrument] = field(default_factory=list)
    excluded: list[ScreeningExclusion] = field(default_factory=list)


def screen_by_liquidity_and_trend(
    instruments: list[Instrument],
    price_history: dict[str, list[MarketBar]],
    criteria: LiquidityTrendScreeningCriteria,
) -> LiquidityTrendScreeningResult:
    """
    Second pass: liquidity floor (mean volume over the supplied history)
    and a simple uptrend filter (last close above the last
    ``criteria.trend_lookback_bars`` bars' simple moving average).

    Deterministic: source-order in, source-order out (before any
    ``max_symbols`` truncation) - matching
    ``CapitalRiskBucketSelector``'s convention.
    """
    result = LiquidityTrendScreeningResult()

    for instrument in instruments:
        bars = price_history.get(instrument.symbol) or []

        if len(bars) < criteria.trend_lookback_bars:
            result.excluded.append(
                ScreeningExclusion(instrument.symbol, instrument.exchange, "insufficient_history")
            )
            continue

        avg_volume = sum(bar.volume for bar in bars) / len(bars)
        if avg_volume < criteria.min_avg_daily_volume:
            result.excluded.append(
                ScreeningExclusion(instrument.symbol, instrument.exchange, "below_liquidity_floor")
            )
            continue

        recent = bars[-criteria.trend_lookback_bars :]
        moving_average = sum(bar.close for bar in recent) / len(recent)
        if bars[-1].close <= moving_average:
            result.excluded.append(
                ScreeningExclusion(instrument.symbol, instrument.exchange, "not_in_uptrend")
            )
            continue

        result.selected.append(instrument)

    if criteria.max_symbols is not None and len(result.selected) > criteria.max_symbols:
        overflow = result.selected[criteria.max_symbols :]
        result.selected = result.selected[: criteria.max_symbols]
        result.excluded.extend(
            ScreeningExclusion(instrument.symbol, instrument.exchange, "max_symbols_cap")
            for instrument in overflow
        )

    return result


@dataclass(slots=True)
class RelativeVolumeScreeningCriteria:
    """
    "Stocks in play" screen: flags a symbol whose most recently completed
    trading day's total volume was unusually high relative to its OWN
    trailing average - a proxy for "something happened recently" (news,
    a catalyst), distinct from ``LiquidityTrendScreeningCriteria``'s
    question of whether a symbol is generally liquid. Research backing
    (see the practicality-audit follow-up conversation): the one academic
    result with real validated edge on intraday momentum/breakout
    restricted to "Stocks in Play" (abnormal same-day volume), not a
    static always-liquid universe.

    Data-grounded default: on this platform's own ~6-month dev-period
    sample, the distribution of (a day's volume / trailing-10-day average
    volume) had a median of ~0.92x and a 90th percentile of ~1.66x -
    ``1.5`` sits just below that 90th percentile, selecting roughly the
    top ~10-15% most active recent days per symbol without being so
    strict signals almost never fire (as tighter thresholds did in the
    earlier parameter sweep on this same platform).
    """

    relative_volume_multiplier: float = 1.5
    trailing_days: int = 10


@dataclass(slots=True)
class RelativeVolumeScreeningResult:
    selected: list[Instrument] = field(default_factory=list)
    excluded: list[ScreeningExclusion] = field(default_factory=list)


def screen_by_relative_volume(
    instruments: list[Instrument],
    price_history: dict[str, list[MarketBar]],
    criteria: RelativeVolumeScreeningCriteria,
) -> RelativeVolumeScreeningResult:
    """
    Groups ``price_history`` into calendar days internally and compares
    the most recently completed day's total volume against the average
    of the ``criteria.trailing_days`` days before that. Needs at least
    ``trailing_days + 1`` distinct days (the "yesterday" being judged,
    plus a full baseline window) - fewer is excluded as
    ``insufficient_history``, matching
    ``screen_by_liquidity_and_trend``'s identical convention.
    """
    result = RelativeVolumeScreeningResult()

    for instrument in instruments:
        bars = price_history.get(instrument.symbol) or []
        volume_by_day: dict[date, float] = {}
        for bar in bars:
            day = bar.timestamp.date()
            volume_by_day[day] = volume_by_day.get(day, 0.0) + bar.volume

        days = sorted(volume_by_day)
        if len(days) < criteria.trailing_days + 1:
            result.excluded.append(
                ScreeningExclusion(instrument.symbol, instrument.exchange, "insufficient_history")
            )
            continue

        most_recent_day = days[-1]
        baseline_days = days[-(criteria.trailing_days + 1) : -1]
        baseline_avg_volume = sum(volume_by_day[d] for d in baseline_days) / len(baseline_days)

        if baseline_avg_volume <= 0 or (
            volume_by_day[most_recent_day] < criteria.relative_volume_multiplier * baseline_avg_volume
        ):
            result.excluded.append(
                ScreeningExclusion(instrument.symbol, instrument.exchange, "not_in_play")
            )
            continue

        result.selected.append(instrument)

    return result

"""
Phase A feature validation: Information-Coefficient testing for the first
batch of candidate features, per docs/research/features/README.md's
four-question filter and AQTP_RESEARCH_STANDARD_v1.md's validation
pipeline.

Cross-sectional stock features (momentum_return, gap_pct, relative_volume,
atr_pct - core.analytics.features): nse_daily_12mo.csv, 129-symbol
universe (122 usable after core.market_data.corporate_actions excludes/
adjusts for real filed actions), ~12 months. One Spearman IC computed PER
DATE across that day's cross-section
of symbols, then the resulting daily-IC series is bootstrapped/
permutation-tested via core.backtest.significance (a daily-IC series is
just another list[float] to that tooling, exactly like VRP's
per-observation spread was).

Index-only features (iv_rv_ratio, pcr): NIFTY/BANKNIFTY, 2016-2026. No
per-stock options data exists in this pipeline (see
docs/research/features/README.md), so these can't be tested
cross-sectionally - tested instead as a single time-series Spearman
correlation between the feature's date-series and the index's own forward
return, via bootstrap_correlation_ci (pairs-bootstrap, not daily-IC, since
there's only one "name" per date).

Run with:
    PYTHONPATH=. python scripts/run_phase_a_feature_validation.py
"""
from __future__ import annotations

import csv
import statistics
from dataclasses import replace
from datetime import date, timedelta
from math import log, sqrt

from core.analytics.benchmark import load_index_spot_series
from core.analytics.features import (
    atr_pct,
    beta,
    gap_pct,
    momentum_return,
    realized_volatility,
    relative_volume,
    session_vwap_distance,
)
from core.backtest.engine import load_bars_from_csv
from core.backtest.significance import (
    bootstrap_correlation_ci,
    bootstrap_mean_ci,
    permutation_test_correlation,
    permutation_test_vs_zero,
    spearman_correlation,
)
from core.market_data.corporate_actions import adjust_bars, load_corporate_actions
from core.strategy.base import MarketBar
from scripts.measure_volatility_risk_premium import collect_near_the_money_iv

CORPORATE_ACTIONS_CSV = "data/processed/nse_corporate_actions.csv"

DAILY_CSV = "data/processed/nse_daily_12mo.csv"
INTRADAY_CSV = "data/processed/nse_5min_dev.csv"
OPTIONS_CSV = "data/processed/nse_index_options_iv_history.csv"
FORWARD_HORIZON_DAYS = 5
MIN_CROSS_SECTION = 8
# The intraday universe is only 10 symbols (9 after corporate-actions
# exclusion, see universe_expansion_2026-07-30.md) - MIN_CROSS_SECTION=8
# would drop nearly every day. Lowered to a majority of what's actually
# available, not the same bar as the 122-symbol daily universe.
MIN_INTRADAY_CROSS_SECTION = 5
FDR_P_MAX = 0.05


def _report(label: str, ci, p_value: float | None, n: int) -> dict:
    survives = ci.lower is not None and ci.lower > 0.0 and p_value is not None and p_value <= FDR_P_MAX
    print(f"--- {label} ---  (n={n})")
    print(f"  point estimate: {ci.point_estimate}")
    print(f"  90% bootstrap CI: [{ci.lower}, {ci.upper}]  (n_resamples={ci.n_resamples})")
    print(f"  permutation p-value: {p_value}")
    print(f"  survives (CI clears 0 AND p<={FDR_P_MAX}): {survives}")
    print()
    return {
        "label": label, "point_estimate": ci.point_estimate, "ci_lower": ci.lower,
        "ci_upper": ci.upper, "p_value": p_value, "n": n, "survives": survives,
    }


# ---------------------------------------------------------------------------
# Cross-sectional stock features
# ---------------------------------------------------------------------------

def _bars_by_symbol(csv_path: str) -> dict[str, list[MarketBar]]:
    """
    Loads bars per symbol, then applies core.market_data.corporate_actions
    against docs/research/data/nse_corporate_actions.csv (see
    docs/research/data/universe_expansion_2026-07-30.md for why this
    matters - SKFINDIA's unadjusted demerger was silently producing a
    fake ~55% "return" before this existed). A symbol with a BONUS/SPLIT
    action in its date range gets its pre-ex-date prices adjusted; a
    symbol with a Rights/Demerger/Merger/unclassified action in range -
    real discontinuity, no computable adjustment factor - is excluded
    entirely rather than left silently wrong.
    """
    bars = load_bars_from_csv(csv_path)
    raw_by_symbol: dict[str, list[MarketBar]] = {}
    for bar in bars:
        raw_by_symbol.setdefault(bar.symbol, []).append(bar)
    for symbol in raw_by_symbol:
        raw_by_symbol[symbol].sort(key=lambda b: b.timestamp)

    actions_by_symbol = load_corporate_actions(CORPORATE_ACTIONS_CSV)

    by_symbol: dict[str, list[MarketBar]] = {}
    excluded: list[str] = []
    adjusted_count = 0
    for symbol, symbol_bars in raw_by_symbol.items():
        adjusted, has_unadjustable = adjust_bars(symbol_bars, actions_by_symbol.get(symbol, []))
        if has_unadjustable:
            excluded.append(symbol)
            continue
        if adjusted is not symbol_bars:
            adjusted_count += 1
        by_symbol[symbol] = adjusted

    print(f"  [corporate actions: {adjusted_count} symbols price-adjusted, "
          f"{len(excluded)} excluded ({', '.join(sorted(excluded)) or 'none'})]")
    return by_symbol


def _nifty_close_by_date(start: date, end: date) -> dict[date, float]:
    return dict(load_index_spot_series(start, end, symbol="NIFTY", csv_path=OPTIONS_CSV))


def validate_beta_feature(label: str, by_symbol: dict[str, list[MarketBar]], lookback_days: int) -> dict:
    """
    Same daily-IC methodology as validate_cross_sectional_feature, but
    beta() needs a second, per-symbol-aligned bars argument
    (compute_feature's single-bars-list signature doesn't fit) - kept as
    its own function rather than complicating that one's signature for
    the only feature in this batch that looks outside the stock's own
    history.
    """
    start = min(b.timestamp.date() for bars in by_symbol.values() for b in bars)
    end = max(b.timestamp.date() for bars in by_symbol.values() for b in bars)
    nifty_by_date = _nifty_close_by_date(start, end)

    by_date: dict[date, list[tuple[float, float]]] = {}
    for symbol, bars in by_symbol.items():
        aligned_stock, aligned_market = [], []
        for bar in bars:
            nifty_close = nifty_by_date.get(bar.timestamp.date())
            if nifty_close is None:
                continue
            aligned_stock.append(bar)
            aligned_market.append(replace(bar, symbol="NIFTY", open=nifty_close, high=nifty_close, low=nifty_close, close=nifty_close))

        for i in range(len(aligned_stock) - FORWARD_HORIZON_DAYS):
            value = beta(aligned_stock[: i + 1], aligned_market[: i + 1], lookback_days=lookback_days)
            if value is None:
                continue
            today_close = aligned_stock[i].close
            if today_close <= 0:
                continue
            forward_return = aligned_stock[i + FORWARD_HORIZON_DAYS].close / today_close - 1.0
            by_date.setdefault(aligned_stock[i].timestamp.date(), []).append((value, forward_return))

    daily_ics: list[float] = []
    for day, pairs in by_date.items():
        if len(pairs) < MIN_CROSS_SECTION:
            continue
        values = [p[0] for p in pairs]
        returns = [p[1] for p in pairs]
        ic = spearman_correlation(values, returns)
        if ic is not None:
            daily_ics.append(ic)

    ci = bootstrap_mean_ci(daily_ics, seed=0)
    p = permutation_test_vs_zero(daily_ics, seed=0) if daily_ics else None
    return _report(f"{label} (cross-sectional daily IC, {FORWARD_HORIZON_DAYS}d fwd return)", ci, p, len(daily_ics))


def _intraday_bars_by_symbol_and_day(csv_path: str) -> dict[str, list[tuple[date, list[MarketBar]]]]:
    """
    Loads intraday bars, applies the same corporate-actions handling as
    _bars_by_symbol (adjust_bars works identically regardless of bar
    frequency - it only cares about symbol/timestamp/price), then groups
    each symbol's bars by trading day, sorted chronologically both across
    days and within each day.
    """
    bars = load_bars_from_csv(csv_path)
    raw_by_symbol: dict[str, list[MarketBar]] = {}
    for bar in bars:
        raw_by_symbol.setdefault(bar.symbol, []).append(bar)
    for symbol in raw_by_symbol:
        raw_by_symbol[symbol].sort(key=lambda b: b.timestamp)

    actions_by_symbol = load_corporate_actions(CORPORATE_ACTIONS_CSV)

    result: dict[str, list[tuple[date, list[MarketBar]]]] = {}
    excluded: list[str] = []
    for symbol, symbol_bars in raw_by_symbol.items():
        adjusted, has_unadjustable = adjust_bars(symbol_bars, actions_by_symbol.get(symbol, []))
        if has_unadjustable:
            excluded.append(symbol)
            continue
        by_day: dict[date, list[MarketBar]] = {}
        for bar in adjusted:
            by_day.setdefault(bar.timestamp.date(), []).append(bar)
        result[symbol] = sorted(by_day.items())

    print(f"  [corporate actions: {len(excluded)} symbols excluded ({', '.join(sorted(excluded)) or 'none'})]")
    return result


def validate_vwap_distance_feature(csv_path: str) -> dict:
    """
    Different data shape from validate_cross_sectional_feature: the
    feature (session_vwap_distance) is computed once PER TRADING DAY from
    that day's own intraday bars, not per-bar - so this groups by
    (symbol, day) first, then runs the same daily-IC methodology on the
    resulting one-value-per-symbol-per-day series.
    """
    by_symbol_day = _intraday_bars_by_symbol_and_day(csv_path)

    by_date: dict[date, list[tuple[float, float]]] = {}
    for symbol, day_list in by_symbol_day.items():
        daily_closes = [day_bars[-1].close for _day, day_bars in day_list]
        for i in range(len(day_list) - FORWARD_HORIZON_DAYS):
            day, day_bars = day_list[i]
            value = session_vwap_distance(day_bars)
            if value is None or daily_closes[i] <= 0:
                continue
            forward_return = daily_closes[i + FORWARD_HORIZON_DAYS] / daily_closes[i] - 1.0
            by_date.setdefault(day, []).append((value, forward_return))

    daily_ics: list[float] = []
    for day, pairs in by_date.items():
        if len(pairs) < MIN_INTRADAY_CROSS_SECTION:
            continue
        ic = spearman_correlation([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            daily_ics.append(ic)

    ci = bootstrap_mean_ci(daily_ics, seed=0)
    p = permutation_test_vs_zero(daily_ics, seed=0) if daily_ics else None
    return _report(f"session_vwap_distance (cross-sectional daily IC, {FORWARD_HORIZON_DAYS}d fwd return)", ci, p, len(daily_ics))


def validate_cross_sectional_feature(
    label: str,
    compute_feature,  # (bars_up_to_and_including_today: list[MarketBar]) -> float | None
    by_symbol: dict[str, list[MarketBar]],
) -> dict:
    # date -> list of (feature_value, forward_return) across symbols that day
    by_date: dict[date, list[tuple[float, float]]] = {}
    for symbol, bars in by_symbol.items():
        for i in range(len(bars) - FORWARD_HORIZON_DAYS):
            value = compute_feature(bars[: i + 1])
            if value is None:
                continue
            today_close = bars[i].close
            if today_close <= 0:
                continue
            forward_return = bars[i + FORWARD_HORIZON_DAYS].close / today_close - 1.0
            by_date.setdefault(bars[i].timestamp.date(), []).append((value, forward_return))

    daily_ics: list[float] = []
    for day, pairs in by_date.items():
        if len(pairs) < MIN_CROSS_SECTION:
            continue
        values = [p[0] for p in pairs]
        returns = [p[1] for p in pairs]
        ic = spearman_correlation(values, returns)
        if ic is not None:
            daily_ics.append(ic)

    ci = bootstrap_mean_ci(daily_ics, seed=0)
    p = permutation_test_vs_zero(daily_ics, seed=0) if daily_ics else None
    return _report(f"{label} (cross-sectional daily IC, {FORWARD_HORIZON_DAYS}d fwd return)", ci, p, len(daily_ics))


# ---------------------------------------------------------------------------
# Index-only features (NIFTY/BANKNIFTY)
# ---------------------------------------------------------------------------

def _put_call_oi_ratio_by_date(csv_path: str, symbol: str) -> dict[date, float]:
    """Aggregate PUT open_interest / aggregate CALL open_interest, all strikes/expiries, per trade_date."""
    put_oi: dict[date, float] = {}
    call_oi: dict[date, float] = {}
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        idx = {name: header.index(name) for name in ("trade_date", "symbol", "option_type", "open_interest")}
        for row in reader:
            if row[idx["symbol"]] != symbol:
                continue
            oi_raw = row[idx["open_interest"]]
            if not oi_raw:
                continue
            trade_date = date.fromisoformat(row[idx["trade_date"]])
            oi = float(oi_raw)
            if row[idx["option_type"]] == "PE":
                put_oi[trade_date] = put_oi.get(trade_date, 0.0) + oi
            elif row[idx["option_type"]] == "CE":
                call_oi[trade_date] = call_oi.get(trade_date, 0.0) + oi

    return {
        d: put_oi[d] / call_oi[d]
        for d in put_oi
        if d in call_oi and call_oi[d] > 0
    }


def _trailing_realized_vol_by_date(spot_series: list[tuple[date, float]], lookback_days: int) -> dict[date, float]:
    """Annualized stdev of trailing log returns, as of each date (backward-looking, unlike VRP's forward-looking target)."""
    result: dict[date, float] = {}
    for i in range(lookback_days, len(spot_series)):
        window = spot_series[i - lookback_days : i + 1]
        log_returns = [log(window[j][1] / window[j - 1][1]) for j in range(1, len(window)) if window[j - 1][1] > 0]
        if len(log_returns) < 3:
            continue
        vol = statistics.stdev(log_returns) * sqrt(252)
        if vol > 0:
            result[spot_series[i][0]] = vol
    return result


def validate_index_feature(label: str, symbol: str, feature_by_date: dict[date, float],
                            spot_series: list[tuple[date, float]]) -> dict:
    spot_by_date = dict(spot_series)
    sorted_dates = sorted(d for d in spot_by_date)
    date_index = {d: i for i, d in enumerate(sorted_dates)}

    feature_values: list[float] = []
    forward_returns: list[float] = []
    for d, value in feature_by_date.items():
        i = date_index.get(d)
        if i is None or i + FORWARD_HORIZON_DAYS >= len(sorted_dates):
            continue
        today_spot = spot_by_date[sorted_dates[i]]
        future_spot = spot_by_date[sorted_dates[i + FORWARD_HORIZON_DAYS]]
        if today_spot <= 0:
            continue
        feature_values.append(value)
        forward_returns.append(future_spot / today_spot - 1.0)

    ci = bootstrap_correlation_ci(feature_values, forward_returns, seed=0)
    p = permutation_test_correlation(feature_values, forward_returns, seed=0) if feature_values else None
    return _report(f"{label} {symbol} ({FORWARD_HORIZON_DAYS}d fwd index return)", ci, p, len(feature_values))


def main() -> None:
    print("=== Cross-sectional stock features (nse_daily_12mo.csv) ===\n")
    by_symbol = _bars_by_symbol(DAILY_CSV)
    results = [
        validate_cross_sectional_feature("momentum_return", lambda bars: momentum_return(bars, lookback_days=20), by_symbol),
        validate_cross_sectional_feature("gap_pct", lambda bars: gap_pct(bars[-1].open, bars[-2].close) if len(bars) >= 2 else None, by_symbol),
        validate_cross_sectional_feature("relative_volume", lambda bars: relative_volume(bars, lookback_days=20), by_symbol),
        validate_cross_sectional_feature("atr_pct", lambda bars: atr_pct(bars, lookback_days=14), by_symbol),
        validate_cross_sectional_feature("realized_volatility", lambda bars: realized_volatility(bars, lookback_days=20), by_symbol),
        validate_beta_feature("beta", by_symbol, lookback_days=60),
    ]

    print("\n=== Intraday-derived feature (nse_5min_dev.csv) ===\n")
    results.append(validate_vwap_distance_feature(INTRADAY_CSV))

    print("\n=== Index-only features (NIFTY/BANKNIFTY) ===\n")
    start, end = date(2016, 1, 1), date(2026, 7, 24)
    for symbol in ("NIFTY", "BANKNIFTY"):
        spot_series = load_index_spot_series(start, end, symbol=symbol, csv_path=OPTIONS_CSV)
        spot_by_date = dict(spot_series)

        iv_by_date = collect_near_the_money_iv(symbol, "PE", target_days=7.0, tolerance_days=3.0)
        rv_by_date = _trailing_realized_vol_by_date(spot_series, lookback_days=20)
        iv_rv_ratio_by_date = {
            d: iv_tte[0] / rv_by_date[d]
            for d, iv_tte in iv_by_date.items()
            if d in rv_by_date and rv_by_date[d] > 0
        }
        results.append(validate_index_feature("iv_rv_ratio", symbol, iv_rv_ratio_by_date, spot_series))

        pcr_by_date = _put_call_oi_ratio_by_date(OPTIONS_CSV, symbol)
        results.append(validate_index_feature("pcr", symbol, pcr_by_date, spot_series))

    print("=== PHASE A SUMMARY (raw p-values, NOT yet FDR-corrected) ===")
    for r in results:
        if r["point_estimate"] is None:
            print(f"{r['label']:60s} SKIPPED (no data)")
            continue
        print(f"{r['label']:60s} IC={r['point_estimate']:+.4f}  "
              f"CI=[{r['ci_lower']:+.4f}, {r['ci_upper']:+.4f}]  "
              f"p={r['p_value']:.4f}  n={r['n']}  "
              f"{'SURVIVES' if r['survives'] else 'FAILS'}")


if __name__ == "__main__":
    main()

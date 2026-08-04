"""
Phase 2 robustness testing for the two Phase A features that survived FDR
correction: iv_rv_ratio (NIFTY, FEAT-0005) and beta (FEAT-0011). Same
holdout/walk-forward/regime/parameter-sensitivity battery VRP and
credit_spread got in Phase 2 (docs/research/validation_audit_phase2.md),
adapted for IC-based feature testing rather than PnL.

iv_rv_ratio: same 2016-2026 NIFTY data as VRP/credit_spread - directly
comparable methodology (dev/holdout split at 2024-01-01, 8 calendar-year
walk-forward windows, IV-level regime split, parameter sensitivity on
realized_vol_lookback_days/forward_horizon_days).

beta: a real data-scoping difference, not hidden - only ~12 months of
daily cross-sectional data exists (vs 10.5 years for the options data),
so "walk-forward" here means 4 sub-periods within that single year, not
multi-year windows. The REGIME split is the actual point of this half:
splits observations by whether NIFTY's own trailing 20-day return was
positive or negative as of that date - directly testing FEAT-0011's
flagged trend-confound concern (is beta's positive IC just reflecting
whichever way the market was already moving?), which a plausibility
check alone couldn't resolve.

Run with:
    PYTHONPATH=. python scripts/run_phase2_feature_robustness_audit.py
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

from core.analytics.features import beta as compute_beta
from core.backtest.significance import (
    bootstrap_correlation_ci,
    bootstrap_mean_ci,
    permutation_test_correlation,
    permutation_test_vs_zero,
    spearman_correlation,
)
from scripts.measure_volatility_risk_premium import collect_near_the_money_iv
from scripts.run_phase_a_feature_validation import (
    DAILY_CSV,
    FORWARD_HORIZON_DAYS,
    MIN_CROSS_SECTION,
    OPTIONS_CSV,
    _bars_by_symbol,
    _nifty_close_by_date,
    _report as report_ic_experiment,
    _trailing_realized_vol_by_date,
)

NIFTY_SYMBOL = "NIFTY"


# ---------------------------------------------------------------------------
# iv_rv_ratio (NIFTY) - same 2016-2026 window as VRP/credit_spread's Phase 2
# ---------------------------------------------------------------------------

DEV_START, DEV_END = date(2016, 1, 1), date(2023, 12, 31)
HOLDOUT_START, HOLDOUT_END = date(2024, 1, 1), date(2026, 7, 24)
WALK_FORWARD_YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]
RV_LOOKBACK_SWEEP = [10, 15, 30, 40]  # default 20 already covered by the holdout/full-window runs
FORWARD_HORIZON_SWEEP = [3, 7, 10]  # default 5 already covered elsewhere


def _iv_rv_ratio_by_date(iv_by_date: dict, spot_series: list[tuple[date, float]], rv_lookback_days: int) -> dict[date, float]:
    # iv_by_date passed in rather than fetched here - collect_near_the_money_iv
    # scans the full 7.4M-row options CSV and doesn't depend on
    # rv_lookback_days at all, so the parameter sweep below would otherwise
    # re-scan it 4 unnecessary times for a value that never changes.
    rv_by_date = _trailing_realized_vol_by_date(spot_series, lookback_days=rv_lookback_days)
    return {
        d: iv_tte[0] / rv_by_date[d]
        for d, iv_tte in iv_by_date.items()
        if d in rv_by_date and rv_by_date[d] > 0
    }


def _correlation_experiment(label: str, feature_by_date: dict[date, float], spot_by_date: dict[date, float], forward_horizon_days: int) -> dict:
    sorted_dates = sorted(spot_by_date)
    date_index = {d: i for i, d in enumerate(sorted_dates)}
    feature_values, forward_returns = [], []
    for d, value in feature_by_date.items():
        i = date_index.get(d)
        if i is None or i + forward_horizon_days >= len(sorted_dates):
            continue
        today_spot = spot_by_date[sorted_dates[i]]
        future_spot = spot_by_date[sorted_dates[i + forward_horizon_days]]
        if today_spot <= 0:
            continue
        feature_values.append(value)
        forward_returns.append(future_spot / today_spot - 1.0)
    ci = bootstrap_correlation_ci(feature_values, forward_returns, seed=0)
    p = permutation_test_correlation(feature_values, forward_returns, seed=0) if feature_values else None
    return report_ic_experiment(label, ci, p, len(feature_values))


def run_iv_rv_ratio_robustness() -> list[dict]:
    spot_series = sorted(_nifty_close_by_date(DEV_START, HOLDOUT_END).items())
    spot_by_date = dict(spot_series)
    iv_by_date = collect_near_the_money_iv(NIFTY_SYMBOL, "PE", target_days=7.0, tolerance_days=3.0)
    feature_by_date = _iv_rv_ratio_by_date(iv_by_date, spot_series, rv_lookback_days=20)

    results = []
    for label, start, end in [
        ("iv_rv_ratio NIFTY DEV", DEV_START, DEV_END),
        ("iv_rv_ratio NIFTY HOLDOUT", HOLDOUT_START, HOLDOUT_END),
    ]:
        restricted = {d: v for d, v in feature_by_date.items() if start <= d <= end}
        results.append(_correlation_experiment(f"{label} {start}-{end}", restricted, spot_by_date, FORWARD_HORIZON_DAYS))

    for year in WALK_FORWARD_YEARS:
        restricted = {d: v for d, v in feature_by_date.items() if d.year == year}
        results.append(_correlation_experiment(f"iv_rv_ratio NIFTY WF-{year}", restricted, spot_by_date, FORWARD_HORIZON_DAYS))

    all_ivs = sorted(feature_by_date.values())
    median_iv_rv = all_ivs[len(all_ivs) // 2]
    for label, predicate in [
        ("HIGH-ratio-regime", lambda v: v >= median_iv_rv),
        ("LOW-ratio-regime", lambda v: v < median_iv_rv),
    ]:
        restricted = {d: v for d, v in feature_by_date.items() if predicate(v)}
        results.append(_correlation_experiment(f"iv_rv_ratio NIFTY {label} (median={median_iv_rv:.3f})", restricted, spot_by_date, FORWARD_HORIZON_DAYS))

    for rv_lookback in RV_LOOKBACK_SWEEP:
        swept_feature = _iv_rv_ratio_by_date(iv_by_date, spot_series, rv_lookback_days=rv_lookback)
        results.append(_correlation_experiment(f"iv_rv_ratio NIFTY rv_lookback={rv_lookback}", swept_feature, spot_by_date, FORWARD_HORIZON_DAYS))

    for horizon in FORWARD_HORIZON_SWEEP:
        results.append(_correlation_experiment(f"iv_rv_ratio NIFTY forward_horizon={horizon}", feature_by_date, spot_by_date, horizon))

    return results


# ---------------------------------------------------------------------------
# beta - real data constraint: only ~12mo of daily cross-sectional data
# exists, so sub-period walk-forward, not multi-year. Regime split is the
# actual point (tests FEAT-0011's flagged trend-confound directly).
# ---------------------------------------------------------------------------

BETA_LOOKBACK_SWEEP = [30, 45, 90, 120]  # default 60 already covered by the full-window run


def compute_beta_daily_ics(by_symbol: dict[str, list], lookback_days: int) -> dict[date, float]:
    """One Spearman IC per trading day, NOT yet bootstrap-tested - callers filter/test subsets of this (holdout, sub-period, regime)."""
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
            value = compute_beta(aligned_stock[: i + 1], aligned_market[: i + 1], lookback_days=lookback_days)
            if value is None:
                continue
            today_close = aligned_stock[i].close
            if today_close <= 0:
                continue
            forward_return = aligned_stock[i + FORWARD_HORIZON_DAYS].close / today_close - 1.0
            by_date.setdefault(aligned_stock[i].timestamp.date(), []).append((value, forward_return))

    daily_ics: dict[date, float] = {}
    for day, pairs in by_date.items():
        if len(pairs) < MIN_CROSS_SECTION:
            continue
        ic = spearman_correlation([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            daily_ics[day] = ic
    return daily_ics


def _test_ic_series(label: str, daily_ics: dict[date, float]) -> dict:
    values = list(daily_ics.values())
    ci = bootstrap_mean_ci(values, seed=0)
    p = permutation_test_vs_zero(values, seed=0) if values else None
    return report_ic_experiment(label, ci, p, len(values))


def _nifty_trailing_return_regime(start: date, end: date, lookback_days: int = 20) -> dict[date, str]:
    spot_series = sorted(_nifty_close_by_date(start, end).items())
    regime: dict[date, str] = {}
    for i in range(lookback_days, len(spot_series)):
        d, close = spot_series[i]
        past_close = spot_series[i - lookback_days][1]
        if past_close <= 0:
            continue
        regime[d] = "UP" if (close / past_close - 1.0) > 0 else "DOWN"
    return regime


def run_beta_robustness(by_symbol: dict[str, list]) -> list[dict]:
    daily_ics = compute_beta_daily_ics(by_symbol, lookback_days=60)
    sorted_days = sorted(daily_ics)
    if not sorted_days:
        return []

    results = []

    # Holdout: split the single ~12mo window roughly in half by trading-day
    # count (not a calendar guess) - a real but scoped-down analog of
    # VRP/credit_spread's multi-year dev/holdout split.
    midpoint = len(sorted_days) // 2
    dev_days, holdout_days = sorted_days[:midpoint], sorted_days[midpoint:]
    results.append(_test_ic_series(f"beta DEV {dev_days[0]}-{dev_days[-1]}", {d: daily_ics[d] for d in dev_days}))
    results.append(_test_ic_series(f"beta HOLDOUT {holdout_days[0]}-{holdout_days[-1]}", {d: daily_ics[d] for d in holdout_days}))

    # Sub-period walk-forward: 4 roughly equal quarters of trading days,
    # not calendar quarters - the real analog of VRP/credit_spread's 8
    # yearly windows, scoped to what 12 months of data actually supports.
    quarter_size = len(sorted_days) // 4
    for q in range(4):
        chunk = sorted_days[q * quarter_size : (q + 1) * quarter_size] if q < 3 else sorted_days[q * quarter_size :]
        if not chunk:
            continue
        results.append(_test_ic_series(f"beta Q{q+1} {chunk[0]}-{chunk[-1]}", {d: daily_ics[d] for d in chunk}))

    # Regime split - the actual point: does beta's positive IC hold in
    # BOTH market directions, or only the one this ~12mo window happened
    # to be in (see FEAT-0011's flagged trend-confound).
    regime_by_date = _nifty_trailing_return_regime(sorted_days[0], sorted_days[-1], lookback_days=20)
    for regime_label in ("UP", "DOWN"):
        restricted = {d: v for d, v in daily_ics.items() if regime_by_date.get(d) == regime_label}
        results.append(_test_ic_series(f"beta NIFTY-{regime_label}-regime (n_days={len(restricted)})", restricted))

    # Parameter sensitivity on the rolling window itself.
    for lookback in BETA_LOOKBACK_SWEEP:
        swept_ics = compute_beta_daily_ics(by_symbol, lookback_days=lookback)
        results.append(_test_ic_series(f"beta lookback_days={lookback}", swept_ics))

    return results


def main() -> None:
    print("########## iv_rv_ratio (NIFTY) robustness ##########\n")
    results = run_iv_rv_ratio_robustness()

    print("\n########## beta robustness ##########\n")
    by_symbol = _bars_by_symbol(DAILY_CSV)
    results += run_beta_robustness(by_symbol)

    print("\n=== PHASE 2 FEATURE ROBUSTNESS SUMMARY ===")
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

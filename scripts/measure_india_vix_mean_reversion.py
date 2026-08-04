"""
Tests whether an ELEVATED India VIX (relative to its own recent trailing
range) predicts positive forward NIFTY returns - a fear-spike/mean-reversion
claim, distinct from EXP-0001/EXP-0002 (VRP: does IV exceed subsequently
REALIZED vol on the SAME underlying's own options) and from FEAT-0005
(iv_rv_ratio: does the IV/RV ratio predict return direction). This is the
first feature test to use India VIX (added 2026-08-05,
core.analytics.benchmark.load_india_vix_series) rather than options-derived
IV - a market-wide fear gauge, not a same-underlying options measure.

Feature definition (fixed BEFORE looking at any result, per this project's
no-data-dredging norm - see docs/research/features/README.md and
FEAT-0001's multi-revision history for why that discipline exists here):

  - Normalize, don't use raw VIX level - raw level is confounded by
    long-run regime (VIX ~14 in 2016-2019 vs. spikes near 30+ in 2020,
    not comparable in absolute terms across a 10-year window). Feature is
    VIX's own PERCENTILE RANK within a trailing 60-trading-day window
    (itself included, i.e. 61 points: today + 60 prior) - same
    ``lookback_days`` convention as this project's other trailing-window
    features (core.analytics.features.beta already uses lookback_days=60;
    reused here rather than picking a new number). 60 days (~3 calendar
    months) is short enough to capture "recent trailing range" rather than
    the full 10-year regime, and long enough that India VIX's ~2,470-bar
    history (2016-08 to present) still leaves ~2,400 usable dates after
    burn-in - a 90-day window was also considered and rejected only for
    burning more history with no stated need, not because it was tried
    and looked worse (it was never run).
  - "Elevated" = percentile rank >= 0.90 (top decile) within that trailing
    window. Top decile, not top quartile, because the fear-spike/rebound
    literature this hypothesis borrows from (US VIX spike-and-reversion
    studies) defines a "spike" as a tail event, not merely an
    above-average reading - top quartile would dilute the effect with mild
    "somewhat elevated" days that aren't spikes. Not tuned against the
    outcome: chosen and written down here before the test below was run.
  - Forward horizon: 5 trading days is PRIMARY, matching this project's
    existing FORWARD_HORIZON_DAYS=5 convention (credit_spread's holding
    period, every feature in run_phase_a_feature_validation.py). A 3-day
    horizon is computed too but reported ONLY as a secondary/exploratory
    check - it does not count toward the pass/fail decision, and was
    always going to be reported regardless of what the primary result
    showed (not added after the fact because 5-day looked weak).

Statistical test: exactly the tooling already used for VRP (a per-
observation value series tested against a zero-edge null) -
core.backtest.significance.bootstrap_mean_ci (90% CI, 2000 resamples) and
permutation_test_vs_zero (2000 sign-flip permutations), both seed=0. The
"value" per observation is NIFTY's forward return on one elevated-VIX day -
same shape as VRP's per-observation (IV - subsequent RV) spread. This is a
directional claim (elevated VIX -> POSITIVE forward return), so the
survival bar is CI lower bound > 0 AND p <= 0.05, matching the bar already
used for VRP and for every feature in run_phase_a_feature_validation.py.

Run with:
    PYTHONPATH=. python scripts/measure_india_vix_mean_reversion.py
"""
from __future__ import annotations

from datetime import date

from core.analytics.benchmark import load_india_vix_series, load_index_spot_series
from core.backtest.significance import bootstrap_mean_ci, permutation_test_vs_zero

VIX_CSV = "data/processed/nse_india_vix_daily.csv"
NIFTY_CSV = "data/processed/nse_index_options_iv_history.csv"

LOOKBACK_DAYS = 60          # trailing window (+ today = 61 points) for the percentile rank
ELEVATED_THRESHOLD = 0.90   # top decile
PRIMARY_HORIZON_DAYS = 5    # matches project-wide FORWARD_HORIZON_DAYS convention
SECONDARY_HORIZON_DAYS = 3  # exploratory only, not part of the decision
FDR_P_MAX = 0.05


def trailing_percentile_ranks(vix_series: list[tuple[date, float]], lookback_days: int) -> dict[date, float]:
    """
    Percentile rank of each date's VIX close within the trailing
    ``lookback_days`` window (that day plus the ``lookback_days`` before
    it - ``lookback_days + 1`` points total, same inclusive convention as
    core.analytics.features' lookback_days functions). Rank = fraction of
    the window's values <= today's value, so a new high in the window
    scores 1.0. First ``lookback_days`` dates have no full window and are
    omitted.
    """
    values = [v for _d, v in vix_series]
    ranks: dict[date, float] = {}
    for i in range(lookback_days, len(vix_series)):
        window = values[i - lookback_days : i + 1]
        rank = sum(1 for w in window if w <= values[i]) / len(window)
        ranks[vix_series[i][0]] = rank
    return ranks


def elevated_dates(percentile_ranks: dict[date, float], threshold: float) -> list[date]:
    return sorted(d for d, r in percentile_ranks.items() if r >= threshold)


def forward_returns(dates: list[date], spot_series: list[tuple[date, float]], horizon_days: int) -> list[float]:
    """NIFTY forward ``horizon_days``-trading-day return as of each date in ``dates``, skipping dates too close to the end of ``spot_series`` to have a forward point."""
    spot_by_date = dict(spot_series)
    sorted_dates = sorted(spot_by_date)
    date_index = {d: i for i, d in enumerate(sorted_dates)}

    returns: list[float] = []
    for d in dates:
        i = date_index.get(d)
        if i is None or i + horizon_days >= len(sorted_dates):
            continue
        today_spot = spot_by_date[sorted_dates[i]]
        if today_spot <= 0:
            continue
        future_spot = spot_by_date[sorted_dates[i + horizon_days]]
        returns.append(future_spot / today_spot - 1.0)
    return returns


def measure() -> dict:
    vix_series = load_india_vix_series(date(2016, 1, 1), date(2030, 1, 1), csv_path=VIX_CSV)
    nifty_series = load_index_spot_series(date(2016, 1, 1), date(2030, 1, 1), symbol="NIFTY", csv_path=NIFTY_CSV)

    ranks = trailing_percentile_ranks(vix_series, LOOKBACK_DAYS)
    elevated = elevated_dates(ranks, ELEVATED_THRESHOLD)

    primary_returns = forward_returns(elevated, nifty_series, PRIMARY_HORIZON_DAYS)
    secondary_returns = forward_returns(elevated, nifty_series, SECONDARY_HORIZON_DAYS)
    all_dates_with_rank = sorted(ranks)
    baseline_returns = forward_returns(all_dates_with_rank, nifty_series, PRIMARY_HORIZON_DAYS)

    ci = bootstrap_mean_ci(primary_returns, seed=0)
    p_value = permutation_test_vs_zero(primary_returns, seed=0) if primary_returns else None
    survives = ci.lower is not None and ci.lower > 0.0 and p_value is not None and p_value <= FDR_P_MAX

    print(f"India VIX dates: {vix_series[0][0]}..{vix_series[-1][0]} (n={len(vix_series)})")
    print(f"NIFTY spot dates: {nifty_series[0][0]}..{nifty_series[-1][0]} (n={len(nifty_series)})")
    print(f"Dates with a full {LOOKBACK_DAYS}-day trailing window: {len(all_dates_with_rank)}")
    print(f"Elevated days (percentile >= {ELEVATED_THRESHOLD}): {len(elevated)} "
          f"({len(elevated) / len(all_dates_with_rank) * 100:.1f}% of eligible days)")
    print()
    print(f"--- PRIMARY: {PRIMARY_HORIZON_DAYS}d forward NIFTY return on elevated-VIX days ---  (n={len(primary_returns)})")
    print(f"  mean forward return: {ci.point_estimate}")
    print(f"  90% bootstrap CI: [{ci.lower}, {ci.upper}]  (n_resamples={ci.n_resamples})")
    print(f"  permutation p-value: {p_value}")
    print(f"  survives (CI lower > 0 AND p<={FDR_P_MAX}): {survives}")
    print()
    baseline_mean = sum(baseline_returns) / len(baseline_returns) if baseline_returns else None
    print(f"[context, not a decision test] unconditional {PRIMARY_HORIZON_DAYS}d mean forward return, all "
          f"{len(baseline_returns)} eligible days: {baseline_mean}")
    secondary_mean = sum(secondary_returns) / len(secondary_returns) if secondary_returns else None
    print(f"[secondary/exploratory, not part of the decision] {SECONDARY_HORIZON_DAYS}d mean forward return "
          f"on the same elevated days (n={len(secondary_returns)}): {secondary_mean}")

    return {
        "n_eligible_days": len(all_dates_with_rank),
        "n_elevated": len(elevated),
        "primary": {
            "point_estimate": ci.point_estimate, "ci_lower": ci.lower, "ci_upper": ci.upper,
            "n_resamples": ci.n_resamples, "p_value": p_value, "n": len(primary_returns), "survives": survives,
        },
        "baseline_mean_5d": baseline_mean,
        "secondary_mean_3d": secondary_mean,
        "secondary_n": len(secondary_returns),
    }


if __name__ == "__main__":
    measure()

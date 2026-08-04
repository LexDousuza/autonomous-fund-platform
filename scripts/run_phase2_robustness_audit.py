"""
Phase 2: temporal & parameter robustness for VRP and vertical_credit_spread
ONLY, per docs/research/AQTP_RESEARCH_STANDARD_v1.md's phase plan - the two
things with a Phase 1 result worth scrutinizing further (VRP validated,
credit_spread inconclusive). The 5 confirmed-loser strategies from Phase 1
don't need this; they're already ruled out on Phase 1's evidence.

Covers, per docs/research/experiments/'s registry fields:
  - holdout: dev (2016-2023) vs. holdout (2024-2026, Phase 1's own window)
  - walk_forward: 8 individual calendar-year windows (2018-2025), same fixed
    config throughout - checking sign consistency of a STATIC strategy
    across years, not a refit-per-window scheme (credit_spread's params
    aren't adaptively fit the way OU's per-bar equilibrium is)
  - regime: IV-level split (above/below the full window's own median IV),
    credit_spread trades joined to their entry date's market IV via VRP's
    own near-the-money IV series (a reasonable regime proxy, not the exact
    contract's own IV - see the module docstring on why this is post-hoc)
  - parameter sensitivity: short_leg_target_delta and iv_rv_ratio_threshold
    swept independently (one at a time) around their config defaults

One HistoricalOptionChainProvider is built once and reused across every
credit_spread call in this script (~56s CSV load paid once, not ~38 times -
see run_credit_spread_backtest's docstring).

Run with:
    PYTHONPATH=. python scripts/run_phase2_robustness_audit.py
"""
from __future__ import annotations

import argparse
import statistics
from datetime import date

from core.backtest.report import build_performance_report
from core.backtest.significance import bootstrap_mean_ci, bootstrap_profit_factor_ci, permutation_test_vs_zero
from core.market_data.historical_option_chain import HistoricalOptionChainProvider
from scripts.backtest_credit_spread_multi_day import CSV_PATH, build_underlying_bars, run_credit_spread_backtest
from scripts.measure_volatility_risk_premium import collect_near_the_money_iv, measure
from scripts.run_phase1_significance_audit import CI_LOWER_BOUND_MIN, FDR_P_MAX, report_experiment

DEV_START, DEV_END = date(2016, 1, 1), date(2023, 12, 31)
HOLDOUT_START, HOLDOUT_END = date(2024, 1, 1), date(2026, 7, 24)
WALK_FORWARD_YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]

# One value swept at a time, default held fixed at the other - not a full
# cross-product grid (28 runs vs. this batch's ~18; the grid's marginal
# value for a 2-parameter strategy this size didn't justify 2-3x the runs).
DELTA_DEFAULT = 0.25
DELTA_SWEEP = [0.15, 0.20, 0.30, 0.35]
THRESHOLD_DEFAULT = 1.2
THRESHOLD_SWEEP = [1.0, 1.1, 1.3, 1.4, 1.5]


def _vrp_series(observations: list[tuple[date, float, float, float]], start: date, end: date) -> list[float]:
    return [vrp for (d, _iv, _rv, vrp) in observations if start <= d <= end]


def vrp_holdout_and_walkforward(symbol: str) -> list[dict]:
    observations = measure(symbol, horizon_days=7.0, tolerance_days=3.0)
    results = []

    dev_vrps = _vrp_series(observations, DEV_START, DEV_END)
    ci = bootstrap_mean_ci(dev_vrps, seed=0)
    p = permutation_test_vs_zero(dev_vrps, seed=0)
    results.append(report_experiment(f"VRP {symbol} DEV {DEV_START}-{DEV_END}", ci, p, threshold=0.0, n_trades=len(dev_vrps)))

    holdout_vrps = _vrp_series(observations, HOLDOUT_START, HOLDOUT_END)
    ci = bootstrap_mean_ci(holdout_vrps, seed=0)
    p = permutation_test_vs_zero(holdout_vrps, seed=0)
    results.append(report_experiment(f"VRP {symbol} HOLDOUT {HOLDOUT_START}-{HOLDOUT_END}", ci, p, threshold=0.0, n_trades=len(holdout_vrps)))

    for year in WALK_FORWARD_YEARS:
        year_vrps = _vrp_series(observations, date(year, 1, 1), date(year, 12, 31))
        ci = bootstrap_mean_ci(year_vrps, seed=0)
        p = permutation_test_vs_zero(year_vrps, seed=0)
        results.append(report_experiment(f"VRP {symbol} WF-{year}", ci, p, threshold=0.0, n_trades=len(year_vrps)))

    all_ivs = sorted(iv for (_d, iv, _rv, _vrp) in observations)
    median_iv = statistics.median(all_ivs)
    high = [vrp for (_d, iv, _rv, vrp) in observations if iv >= median_iv]
    low = [vrp for (_d, iv, _rv, vrp) in observations if iv < median_iv]
    for label, subset in [("HIGH-IV-regime", high), ("LOW-IV-regime", low)]:
        ci = bootstrap_mean_ci(subset, seed=0)
        p = permutation_test_vs_zero(subset, seed=0)
        results.append(report_experiment(f"VRP {symbol} {label} (median={median_iv:.4f})", ci, p, threshold=0.0, n_trades=len(subset)))

    return results


def _credit_spread_window(
    provider: HistoricalOptionChainProvider, symbol: str, start: date, end: date,
    label: str, strategy_overrides: dict | None = None, max_holding_days: int = 5,
) -> dict:
    bars = build_underlying_bars(symbol, start, end)
    results = run_credit_spread_backtest(
        bars=bars, symbol=symbol, option_type="PUT", start=start,
        option_chain_provider=provider, strategy_overrides=strategy_overrides,
        max_holding_days=max_holding_days,
    )
    report = build_performance_report(results)
    pnls = [t.realized_pnl for t in report.closed_trades]
    ci = bootstrap_profit_factor_ci(pnls, seed=0)
    p = permutation_test_vs_zero(pnls, seed=0) if pnls else None
    return report_experiment(label, ci, p, threshold=CI_LOWER_BOUND_MIN, n_trades=len(pnls)), report.closed_trades


def credit_spread_holdout_and_walkforward(
    provider: HistoricalOptionChainProvider, symbol: str, max_holding_days: int = 5,
) -> tuple[list[dict], list]:
    # Suffix only appears for a non-default hold - keeps the default 5-day
    # run's printed labels byte-identical to before this parameter existed.
    hold_suffix = f" hold={max_holding_days}d" if max_holding_days != 5 else ""
    results = []
    dev_result, _ = _credit_spread_window(
        provider, symbol, DEV_START, DEV_END, f"credit_spread {symbol} DEV {DEV_START}-{DEV_END}{hold_suffix}",
        max_holding_days=max_holding_days,
    )
    results.append(dev_result)

    holdout_result, holdout_trades = _credit_spread_window(
        provider, symbol, HOLDOUT_START, HOLDOUT_END, f"credit_spread {symbol} HOLDOUT {HOLDOUT_START}-{HOLDOUT_END}{hold_suffix}",
        max_holding_days=max_holding_days,
    )
    results.append(holdout_result)

    for year in WALK_FORWARD_YEARS:
        wf_result, _ = _credit_spread_window(
            provider, symbol, date(year, 1, 1), date(year, 12, 31), f"credit_spread {symbol} WF-{year}{hold_suffix}",
            max_holding_days=max_holding_days,
        )
        results.append(wf_result)

    return results, holdout_trades


def credit_spread_regime_split(symbol: str, full_window_trades: list, max_holding_days: int = 5) -> list[dict]:
    iv_by_date = collect_near_the_money_iv(symbol, "PE", target_days=7.0, tolerance_days=3.0)
    if not iv_by_date:
        return []
    median_iv = statistics.median(iv for (iv, _tte) in iv_by_date.values())

    high_pnls, low_pnls, unmatched = [], [], 0
    for trade in full_window_trades:
        entry = iv_by_date.get(trade.trading_date)
        if entry is None:
            unmatched += 1
            continue
        iv, _tte = entry
        (high_pnls if iv >= median_iv else low_pnls).append(trade.realized_pnl)

    hold_suffix = f" hold={max_holding_days}d" if max_holding_days != 5 else ""
    print(f"  [credit_spread {symbol} regime split: {unmatched}/{len(full_window_trades)} trades had no IV match on their entry date - excluded]")
    results = []
    for label, pnls in [("HIGH-IV-regime", high_pnls), ("LOW-IV-regime", low_pnls)]:
        ci = bootstrap_profit_factor_ci(pnls, seed=0)
        p = permutation_test_vs_zero(pnls, seed=0) if pnls else None
        results.append(report_experiment(f"credit_spread {symbol} {label} (median={median_iv:.4f}){hold_suffix}", ci, p, threshold=CI_LOWER_BOUND_MIN, n_trades=len(pnls)))
    return results


def credit_spread_parameter_sensitivity(provider: HistoricalOptionChainProvider, symbol: str) -> list[dict]:
    results = []
    for delta in DELTA_SWEEP:
        r, _ = _credit_spread_window(
            provider, symbol, HOLDOUT_START, HOLDOUT_END,
            f"credit_spread {symbol} delta={delta}",
            strategy_overrides={"short_leg_target_delta": delta},
        )
        results.append(r)
    for threshold in THRESHOLD_SWEEP:
        r, _ = _credit_spread_window(
            provider, symbol, HOLDOUT_START, HOLDOUT_END,
            f"credit_spread {symbol} iv_rv_threshold={threshold}",
            strategy_overrides={"iv_rv_ratio_threshold": threshold},
        )
        results.append(r)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--max-holding-days", type=int, default=5,
        help="credit_spread's max holding period in days, forwarded to every holdout/walk-forward/"
             "regime-split window in this run (default 5, the original hardcoded value - see "
             "docs/research/validation_audit_phase2_longer_hold.md for why this became a parameter).",
    )
    parser.add_argument(
        "--skip-parameter-sensitivity", action="store_true",
        help="skip the short_leg_target_delta/iv_rv_ratio_threshold sweep - already run and recorded "
             "at the 5-day default (validation_audit_phase2.md); not the variable under test when "
             "re-running this battery at a different --max-holding-days.",
    )
    args = parser.parse_args()

    print("=== Building shared option chain provider (one-time ~56s CSV load) ===")
    provider = HistoricalOptionChainProvider(csv_path=CSV_PATH, lot_size=75)

    all_results = []
    for symbol in ("NIFTY", "BANKNIFTY"):
        print(f"\n########## {symbol}: VRP holdout/walk-forward/regime ##########")
        all_results += vrp_holdout_and_walkforward(symbol)

        print(f"\n########## {symbol}: credit_spread holdout/walk-forward (hold={args.max_holding_days}d) ##########")
        cs_results, holdout_trades = credit_spread_holdout_and_walkforward(
            provider, symbol, max_holding_days=args.max_holding_days,
        )
        all_results += cs_results

        print(f"\n########## {symbol}: credit_spread regime split (from HOLDOUT window trades) ##########")
        all_results += credit_spread_regime_split(symbol, holdout_trades, max_holding_days=args.max_holding_days)

        if not args.skip_parameter_sensitivity:
            print(f"\n########## {symbol}: credit_spread parameter sensitivity ##########")
            all_results += credit_spread_parameter_sensitivity(provider, symbol)

    print("\n=== PHASE 2 SUMMARY ===")
    for r in all_results:
        if r["point_estimate"] is None:
            print(f"{r['label']:55s} SKIPPED (no trades)")
            continue
        print(f"{r['label']:55s} point={r['point_estimate']:+.4f}  "
              f"CI=[{r['ci_lower']:+.4f}, {r['ci_upper']:+.4f}]  "
              f"p={r['p_value']:.4f}  n={r['n_trades']}  "
              f"{'SURVIVES' if r['ci_survives'] and r['p_survives'] else 'FAILS'}")


if __name__ == "__main__":
    main()

"""
Phase 1 re-audit: run core.backtest.significance against the real results
already claimed for VRP and vertical_credit_spread, using the pre-registered
kill criteria in docs/research/AQTP_RESEARCH_STANDARD_v1.md.

This covers VRP and credit-spread only - see
docs/research/experiments/README.md for why the Benjamini-Hochberg
correction across ALL previously-tested strategy configs (OU included) is a
separate step once their results are gathered too; running it on a subset
now would under-correct.

Run with:
    PYTHONPATH=. python scripts/run_phase1_significance_audit.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date

from core.backtest.report import build_performance_report
from core.backtest.significance import (
    bootstrap_mean_ci,
    bootstrap_profit_factor_ci,
    permutation_test_vs_zero,
)
from scripts.backtest_credit_spread_multi_day import build_underlying_bars, run_credit_spread_backtest
from scripts.measure_volatility_risk_premium import measure

# Kill criteria from docs/research/AQTP_RESEARCH_STANDARD_v1.md - PROPOSED,
# not yet confirmed by the research reviewer. See that doc for why running
# ahead of confirmation was accepted as a tradeoff rather than blocking.
CI_LOWER_BOUND_MIN = 1.0  # profit factor; VRP checked against 0.0 (a mean, not a ratio)
FDR_P_MAX = 0.05

_TRADE_LOG_PNL = re.compile(r"pnl=(-?\d+\.\d+)")

# Same real dataset per bar-granularity across every strategy in this batch,
# rather than each strategy's own (inconsistently documented) historical
# choice - a uniform, disclosed convention makes the significance
# comparison across strategies fair. csv, not synthetic (apps/backtester's
# default) - see this script's module docstring.
_5MIN_CSV = "data/processed/nse_5min_dev.csv"
_1MIN_CSV = "data/processed/nse_1min_recent.csv"

# (config filename, csv path). simple_breakout_momentum's existing tuning
# was synthetic-only (see its own config comment) - this is that
# strategy's first real-data evaluation, not a re-audit of a prior real
# result.
GENERIC_BACKTESTER_CONFIGS: list[tuple[str, str]] = [
    ("momentum.yaml", _5MIN_CSV),
    ("ma_crossover.yaml", _5MIN_CSV),
    ("vwap_reversion.yaml", _5MIN_CSV),
    ("pullback.yaml", _5MIN_CSV),
    ("relative_volume_pullback.yaml", _5MIN_CSV),
    ("simple_breakout_momentum.yaml", _5MIN_CSV),
    ("ornstein_uhlenbeck_5min.yaml", _5MIN_CSV),
    ("ornstein_uhlenbeck_pair.yaml", _5MIN_CSV),
    ("ornstein_uhlenbeck_residual_5min.yaml", _5MIN_CSV),
    ("gbm_momentum_5min.yaml", _5MIN_CSV),
    ("ornstein_uhlenbeck.yaml", _1MIN_CSV),
    ("ornstein_uhlenbeck_pair_1min.yaml", _1MIN_CSV),
    ("ornstein_uhlenbeck_residual.yaml", _1MIN_CSV),
    ("gbm_momentum.yaml", _1MIN_CSV),
]


def report_experiment(label: str, ci, p_value: float | None, threshold: float, n_trades: int | None = None) -> dict:
    if ci.point_estimate is None:
        print(f"--- {label} ---")
        print(f"  {'no trades generated' if not n_trades else 'too few observations for a CI'} - skipped")
        print()
        return {
            "label": label, "point_estimate": None, "ci_lower": None, "ci_upper": None,
            "p_value": None, "ci_survives": False, "p_survives": False, "n_trades": n_trades,
        }

    ci_survives = ci.lower is not None and ci.lower > threshold
    p_survives = p_value is not None and p_value <= FDR_P_MAX
    print(f"--- {label} ---" + (f"  (n_trades={n_trades})" if n_trades is not None else ""))
    print(f"  point estimate: {ci.point_estimate}")
    print(f"  90% bootstrap CI: [{ci.lower}, {ci.upper}]  (n={ci.n_resamples})")
    print(f"  permutation p-value: {p_value}")
    print(f"  kill criteria: CI lower > {threshold}: {ci_survives}  |  p <= {FDR_P_MAX}: {p_survives}")
    print()
    return {
        "label": label,
        "point_estimate": ci.point_estimate,
        "ci_lower": ci.lower,
        "ci_upper": ci.upper,
        "p_value": p_value,
        "ci_survives": ci_survives,
        "p_survives": p_value is not None and p_value <= FDR_P_MAX,
        "n_trades": n_trades,
    }


def audit_vrp(symbol: str) -> dict:
    observations = measure(symbol, horizon_days=7.0, tolerance_days=3.0)
    vrps = [o[3] for o in observations]
    ci = bootstrap_mean_ci(vrps, seed=0)
    p_value = permutation_test_vs_zero(vrps, seed=0)
    return report_experiment(f"VRP {symbol}", ci, p_value, threshold=0.0)


def audit_credit_spread(symbol: str) -> dict:
    start, end = date(2024, 1, 1), date(2026, 7, 24)
    bars = build_underlying_bars(symbol, start, end)
    results = run_credit_spread_backtest(bars=bars, symbol=symbol, option_type="PUT", start=start)
    report = build_performance_report(results)
    pnls = [t.realized_pnl for t in report.closed_trades]
    ci = bootstrap_profit_factor_ci(pnls, seed=0)
    p_value = permutation_test_vs_zero(pnls, seed=0)
    return report_experiment(f"credit_spread {symbol}", ci, p_value, threshold=CI_LOWER_BOUND_MIN)


def audit_via_generic_backtester(config_filename: str, csv_path: str) -> dict:
    """
    Runs apps/backtester/run_backtest.py as a subprocess (BACKTEST_DATA_SOURCE=csv,
    real data - never the synthetic default) and parses its printed trade
    log for PnLs. Subprocess + parse rather than importing/refactoring
    run_backtest.main() directly: that function's bucket-selection and
    per-strategy market-proxy injection logic is intricate enough that a
    clean extraction risks changing its behavior, where this needs nothing
    from it but the final PnL list.
    """
    label = f"{config_filename.removesuffix('.yaml')} [{os.path.basename(csv_path)}]"
    env = {
        **os.environ,
        "BACKTEST_DATA_SOURCE": "csv",
        "BACKTEST_CSV_PATH": csv_path,
        "BACKTEST_STRATEGY_CONFIG_PATH": f"config/strategies/{config_filename}",
    }
    proc = subprocess.run(
        [sys.executable, "apps/backtester/run_backtest.py"],
        env=env, capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        print(f"--- {label} ---")
        print(f"  FAILED (exit {proc.returncode}): {proc.stderr[-800:]}")
        print()
        return {"label": label, "point_estimate": None, "ci_lower": None, "ci_upper": None,
                "p_value": None, "ci_survives": False, "p_survives": False, "n_trades": None}

    pnls = [float(m) for m in _TRADE_LOG_PNL.findall(proc.stdout)]
    ci = bootstrap_profit_factor_ci(pnls, seed=0)
    p_value = permutation_test_vs_zero(pnls, seed=0) if pnls else None
    return report_experiment(label, ci, p_value, threshold=CI_LOWER_BOUND_MIN, n_trades=len(pnls))


def main() -> None:
    results = [
        audit_vrp("NIFTY"),
        audit_vrp("BANKNIFTY"),
        audit_credit_spread("NIFTY"),
        audit_credit_spread("BANKNIFTY"),
    ]
    for config_filename, csv_path in GENERIC_BACKTESTER_CONFIGS:
        results.append(audit_via_generic_backtester(config_filename, csv_path))

    print("=== SUMMARY (raw p-values, NOT yet FDR-corrected) ===")
    for r in results:
        if r["point_estimate"] is None:
            print(f"{r['label']:45s} SKIPPED (no trades or failed)")
            continue
        survives = "SURVIVES (raw)" if r["ci_survives"] and r["p_survives"] else "FAILS"
        print(f"{r['label']:45s} point={r['point_estimate']:+.4f}  "
              f"CI=[{r['ci_lower']:+.4f}, {r['ci_upper']:+.4f}]  p={r['p_value']:.4f}  {survives}")


if __name__ == "__main__":
    main()

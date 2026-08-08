"""
Final step of the vectorbt independent cross-check (Track 3 of
docs/roadmap.md). Runs back in this project's own venv (needs
core.backtest.costs/significance - pure, broker-independent modules,
no pandas-version conflict) against
backtesting/gap_continuation_vectorbt_crosscheck/vectorbt_trades.csv -
the trade list scripts/vectorbt_cross_check_gap_continuation.py produced
in the isolated venv.

Applies this platform's own REAL Indian delivery-equity cost model
(core.backtest.costs.compute_round_trip_costs) and the same
bootstrap/permutation tooling every other result in this registry uses
(core.backtest.significance) - deliberately NOT reimplemented inside
vectorbt (see that script's docstring: the cost model isn't what's being
cross-checked, the simulation ENGINE is), so the resulting net PF is
directly comparable, apples-to-apples, against
data/processed/gap_event_walk_forward_results.json's own holdout net PF
(1.281, CI [1.200,1.365]).

Run with:
    PYTHONPATH=. python scripts/analyze_vectorbt_crosscheck_results.py
"""
from __future__ import annotations

import csv
import json

from core.backtest.costs import compute_round_trip_costs
from core.backtest.significance import bootstrap_profit_factor_ci, permutation_test_vs_zero

TRADES_CSV = "backtesting/gap_continuation_vectorbt_crosscheck/vectorbt_trades.csv"
HOLDOUT_RESULTS_JSON = "data/processed/gap_event_walk_forward_results.json"
SUMMARY_RESULTS_JSON = "data/processed/gap_continuation_vectorbt_crosscheck_results.json"


def main() -> None:
    with open(TRADES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"{TRADES_CSV} has no trades - run scripts/vectorbt_cross_check_gap_continuation.py "
                          f"in the isolated venv first.")

    gross_pnls, net_pnls = [], []
    for row in rows:
        entry_price, exit_price, quantity = float(row["entry_price"]), float(row["exit_price"]), float(row["quantity"])
        gross_pnl = (exit_price - entry_price) * quantity
        costs = compute_round_trip_costs(entry_price, exit_price, quantity, delivery=True)
        gross_pnls.append(gross_pnl)
        net_pnls.append(gross_pnl - costs)

    total_costs = sum(g - n for g, n in zip(gross_pnls, net_pnls))
    gross_ci = bootstrap_profit_factor_ci(gross_pnls, seed=0)
    net_ci = bootstrap_profit_factor_ci(net_pnls, seed=0)
    net_p = permutation_test_vs_zero(net_pnls, seed=0)

    print(f"=== VECTORBT CROSS-CHECK (independent second engine) ===")
    print(f"n={len(rows):,} trades  (position-tracked: one open position per symbol at a time - see "
          f"scripts/vectorbt_cross_check_gap_continuation.py's own docstring on why this is typically <= "
          f"the original harness's overlap-permitting trade count)")
    print(f"Total gross PnL: {sum(gross_pnls):,.2f}  |  Total costs: {total_costs:,.2f}  |  "
          f"Total net PnL: {sum(net_pnls):,.2f}")
    print(f"Gross profit factor: {gross_ci.point_estimate:.3f}  CI=[{gross_ci.lower:.3f}, {gross_ci.upper:.3f}]")
    print(f"Net profit factor:   {net_ci.point_estimate:.3f}  CI=[{net_ci.lower:.3f}, {net_ci.upper:.3f}]  p={net_p:.4f}")

    with open(HOLDOUT_RESULTS_JSON, encoding="utf-8") as f:
        holdout = json.load(f)["holdout"]
    original_net_pf = holdout["net_pf"]
    print(f"\n=== ORIGINAL HARNESS (scripts/backtest_gap_event_walk_forward.py, same HOLDOUT period) ===")
    print(f"n={holdout['n']:,} trades")
    print(f"Net profit factor:   {original_net_pf['point_estimate']:.3f}  "
          f"CI=[{original_net_pf['lower']:.3f}, {original_net_pf['upper']:.3f}]")

    print(f"\n=== AGREEMENT ===")
    same_sign_of_edge = (net_ci.lower is not None and net_ci.lower > 1) == (original_net_pf["lower"] > 1)
    overlapping_ci = not (net_ci.upper < original_net_pf["lower"] or net_ci.lower > original_net_pf["upper"])
    print(f"Both CIs entirely above 1 (real, cost-surviving edge in both engines): {same_sign_of_edge}")
    print(f"Confidence intervals overlap: {overlapping_ci}")
    if same_sign_of_edge and overlapping_ci:
        print("Independent engines agree: this is real evidence, not an artifact of the original harness.")
    else:
        print("DISAGREEMENT - investigate before trusting either number further. Known, disclosed methodology "
              "differences (position-overlap handling, trade-matching) may explain a magnitude gap, but a SIGN "
              "disagreement (one engine finds a real edge, the other doesn't) would not be explained by those "
              "alone and needs a real bug hunt.")

    with open(SUMMARY_RESULTS_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "engine": "vectorbt (isolated venv - see script docstrings for why)",
            "n_trades": len(rows),
            "net_pf": {"point_estimate": net_ci.point_estimate, "lower": net_ci.lower, "upper": net_ci.upper},
            "gross_pf": {"point_estimate": gross_ci.point_estimate, "lower": gross_ci.lower, "upper": gross_ci.upper},
            "net_p_value": net_p,
            "original_harness_net_pf": original_net_pf,
            "original_harness_n_trades": holdout["n"],
            "cis_overlap": overlapping_ci,
            "both_entirely_above_1": same_sign_of_edge,
            "known_methodology_difference": (
                "vectorbt.Portfolio.from_signals allows only one open position per symbol at a time; "
                "the original harness has no position-tracking and permits overlapping trades - this "
                "cross-check's n is expected to be <= the original's for that reason alone"
            ),
        }, f, indent=2)
    print(f"\nSummary written to {SUMMARY_RESULTS_JSON}")


if __name__ == "__main__":
    main()

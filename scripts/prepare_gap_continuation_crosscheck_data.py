"""
Data-prep step for the vectorbt independent cross-check (Track 3 of
docs/roadmap.md, backtesting/gap_continuation_vectorbt_crosscheck/).

Runs in THIS project's own venv (needs core.market_data's full import
chain - it eagerly pulls in the broker-transport stack, which needs
growwapi/kiteconnect/smartapi-python and their own pandas<3.0 pin) so it
can reuse this platform's already-adjusted bars and BONUS/SPLIT exclusion
unchanged. vectorbt needs pandas>=3.0.3 - a hard, verified conflict with
growwapi's pandas<3.0.0 requirement - so the actual vectorbt simulation
runs in a SEPARATE, isolated venv
(backtesting/gap_continuation_vectorbt_crosscheck/.venv) that never
imports core.market_data at all. This script is the handoff: dump the
adjusted bars + the DEV-derived threshold to plain CSV/JSON so the
isolated venv's simulation script has everything it needs without ever
touching this project's broker-dependent import chain.

Deliberately reuses (not reimplements) this platform's own data loading
and BONUS/SPLIT-overlap logic - that machinery is already separately
audited (docs/research/data_quality_audit_phase1.md,
corporate_action_adjustment_double_counting.md) and is shared input, not
what this cross-check exists to verify. Only the entry/exit SIMULATION
(scripts/vectorbt_cross_check_gap_continuation.py, run in the isolated
venv) is the independently-built second engine.

Run with:
    PYTHONPATH=. python scripts/prepare_gap_continuation_crosscheck_data.py
"""
from __future__ import annotations

import csv
import json

from core.market_data.corporate_actions import load_corporate_actions
from scripts.backtest_gap_event_walk_forward import SPLIT_DATE, compute_dev_threshold
from scripts.measure_corporate_action_drift import (
    CORPORATE_ACTIONS_CSV,
    FORWARD_HORIZON_DAYS,
    PLACEBO_LOOKBACK_TRADING_DAYS,
    _primary_adjusted_bars,
)
from scripts.measure_dividend_drift import _has_bonus_split_in_span

BARS_CSV = "backtesting/gap_continuation_vectorbt_crosscheck/adjusted_bars.csv"
META_JSON = "backtesting/gap_continuation_vectorbt_crosscheck/crosscheck_meta.json"


def main() -> None:
    print("Loading + adjusting the combined universe (same data every gap_event script uses)...")
    primary = _primary_adjusted_bars()
    actions_by_symbol = load_corporate_actions(CORPORATE_ACTIONS_CSV)
    dev_threshold = compute_dev_threshold(primary)
    print(f"DEV-derived gap threshold (unchanged from backtest_gap_event_walk_forward.py): {dev_threshold:.5f}")

    rows_written = 0
    with open(BARS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "date", "open", "close", "entry_eligible", "bonus_split_overlap"])
        for symbol, bars in primary.items():
            symbol_actions = actions_by_symbol.get(symbol, [])
            has_enough_history = len(bars) > PLACEBO_LOOKBACK_TRADING_DAYS + FORWARD_HORIZON_DAYS + 1

            # Write EVERY bar (not just ones eligible to be an entry) - a
            # bar near the end of one symbol's history is still needed as
            # the EXIT price for an entry FORWARD_HORIZON_DAYS earlier.
            # Restricting this file to "valid entry" rows only (an earlier
            # version of this script did) silently drops exactly the exit
            # bars those entries need, since exit_idx = entry_idx +
            # FORWARD_HORIZON_DAYS can land past the entry-eligible range.
            for i, bar in enumerate(bars):
                eligible = (
                    has_enough_history
                    and PLACEBO_LOOKBACK_TRADING_DAYS + 1 <= i < len(bars) - FORWARD_HORIZON_DAYS
                )
                overlap = False
                if eligible:
                    # Same BONUS/SPLIT-overlap span every sibling gap_event
                    # script checks - only meaningful (and only computed)
                    # for a bar that could actually be an entry.
                    placebo_entry_idx = i - PLACEBO_LOOKBACK_TRADING_DAYS
                    span_start = bars[placebo_entry_idx].timestamp.date()
                    span_end = bars[i + FORWARD_HORIZON_DAYS].timestamp.date()
                    overlap = _has_bonus_split_in_span(symbol_actions, span_start, span_end)
                writer.writerow([symbol, bar.timestamp.date().isoformat(), bar.open, bar.close, int(eligible), int(overlap)])
                rows_written += 1

    with open(META_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "dev_threshold": dev_threshold,
            "split_date": SPLIT_DATE.isoformat(),
            "forward_horizon_days": FORWARD_HORIZON_DAYS,
            "capital_per_position": 10_000.0,
        }, f, indent=2)

    print(f"Wrote {rows_written} bar-rows to {BARS_CSV}")
    print(f"Wrote metadata to {META_JSON}")


if __name__ == "__main__":
    main()

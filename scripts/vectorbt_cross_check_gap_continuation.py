"""
Independent second backtest engine (vectorbt) for TRANS-0007/
gap_continuation's up-gap-continuation finding (Track 3 of
docs/roadmap.md): "agreement with your own harness is real evidence;
disagreement is a bug worth finding now."

Runs in an ISOLATED venv
(backtesting/gap_continuation_vectorbt_crosscheck/.venv, NOT this
project's own .venv - vectorbt needs pandas>=3.0.3, a hard, verified
conflict with growwapi's pandas<3.0.0 pin) against the CSV
scripts/prepare_gap_continuation_crosscheck_data.py already dumped from
this platform's own already-adjusted bars - see that script's docstring
for why data loading is shared input here, not what this cross-check
exists to verify.

KNOWN METHODOLOGICAL DIFFERENCE from
scripts/backtest_gap_event_walk_forward.py: that script has no position-
tracking at all - every qualifying gap day becomes its own trade
regardless of whether an earlier trade on the same symbol is still
"open" FORWARD_HORIZON_DAYS later, so two trades CAN overlap.
vectorbt.Portfolio.from_signals's standard semantics allow only one open
position per symbol at a time (a later entry signal while already in a
position is simply skipped) - arguably the more realistic convention,
but a genuine, disclosed difference, not a bug to paper over. Expect
this cross-check's trade count to be <= the original's for that reason
alone, separate from any real disagreement.

fees=0 inside vectorbt deliberately - the platform's own real Indian
delivery-equity cost model (core.backtest.costs.compute_round_trip_costs)
is applied AFTERWARD, back in the main venv, on this script's own
extracted trade list (vectorbt_trades.csv) - see
scripts/analyze_vectorbt_crosscheck_results.py. That cost model isn't
what's being cross-checked; this simulation ENGINE (signal-to-trade
extraction/accounting) is.

Run with (from the isolated venv):
    backtesting/gap_continuation_vectorbt_crosscheck/.venv/bin/python \\
        scripts/vectorbt_cross_check_gap_continuation.py
"""
from __future__ import annotations

import json

import pandas as pd
import vectorbt as vbt

BARS_CSV = "backtesting/gap_continuation_vectorbt_crosscheck/adjusted_bars.csv"
META_JSON = "backtesting/gap_continuation_vectorbt_crosscheck/crosscheck_meta.json"
TRADES_CSV = "backtesting/gap_continuation_vectorbt_crosscheck/vectorbt_trades.csv"


def simulate_symbol(symbol: str, g: pd.DataFrame, dev_threshold: float, split_date: pd.Timestamp,
                     forward_horizon_days: int, capital_per_position: float) -> pd.DataFrame | None:
    g = g.sort_values("date").set_index("date")
    if len(g) < forward_horizon_days + 2:
        return None

    close = g["close"]
    gap = g["open"] / close.shift(1) - 1

    entries = (
        (gap >= dev_threshold)
        & g["entry_eligible"].astype(bool)
        & (~g["bonus_split_overlap"].astype(bool))
        & (g.index >= split_date)
    ).fillna(False)
    if not entries.any():
        return None

    # Series.shift() on a bool column upcasts to object dtype at the NaN
    # boundary it introduces - .fillna(False) alone doesn't cast it back,
    # and vectorbt's numba-compiled internals reject an object-dtype
    # array outright (non-precise type array(pyobject, ...)).
    exits = entries.shift(forward_horizon_days).fillna(False).astype(bool)

    portfolio = vbt.Portfolio.from_signals(
        close, entries, exits,
        # init_cash matters here: vectorbt's default (100.0) is far below
        # capital_per_position, and size_type="value" silently CAPS each
        # position to whatever cash is available rather than erroring -
        # every trade was getting sized to ~Rs 100 notional instead of
        # the requested Rs 10,000 until this was set explicitly (caught
        # by comparing this script's own trade list against the entry/
        # exit price pairs by hand). 10x headroom, not exactly
        # capital_per_position, since only one position is open per
        # symbol at a time but this is a fresh Portfolio per symbol
        # regardless - no real capital-sharing constraint being modeled
        # here, just avoiding the silent truncation.
        init_cash=capital_per_position * 10,
        size=capital_per_position, size_type="value",
        fees=0.0, direction="longonly", freq="1D",
    )
    trades = portfolio.trades.records_readable
    if len(trades) == 0:
        return None

    result = trades[["Entry Timestamp", "Avg Entry Price", "Exit Timestamp", "Avg Exit Price", "Size"]].copy()
    result.columns = ["entry_date", "entry_price", "exit_date", "exit_price", "quantity"]
    result.insert(0, "symbol", symbol)
    return result


def main() -> None:
    with open(META_JSON, encoding="utf-8") as f:
        meta = json.load(f)
    dev_threshold = meta["dev_threshold"]
    split_date = pd.Timestamp(meta["split_date"])
    forward_horizon_days = meta["forward_horizon_days"]
    capital_per_position = meta["capital_per_position"]

    print(f"Loading {BARS_CSV}...")
    df = pd.read_csv(BARS_CSV, parse_dates=["date"])
    print(f"{len(df):,} bar-rows, {df['symbol'].nunique():,} symbols. DEV threshold={dev_threshold:.5f}")

    all_trades, n_symbols_with_trades = [], 0
    for symbol, g in df.groupby("symbol", sort=False):
        result = simulate_symbol(symbol, g, dev_threshold, split_date, forward_horizon_days, capital_per_position)
        if result is not None:
            all_trades.append(result)
            n_symbols_with_trades += 1

    combined = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame(
        columns=["symbol", "entry_date", "entry_price", "exit_date", "exit_price", "quantity"]
    )
    combined.to_csv(TRADES_CSV, index=False)

    print(f"\n{n_symbols_with_trades:,} symbols produced at least one HOLDOUT-period trade.")
    print(f"Total trades (vectorbt's own position-tracked count, one position per symbol at a time): {len(combined):,}")
    print(f"Wrote {TRADES_CSV}")


if __name__ == "__main__":
    main()

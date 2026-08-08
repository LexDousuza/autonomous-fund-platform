"""
Forward-validation tripwire (Track 1 of docs/roadmap.md): once a
strategy's forward (paper or live) trade count reaches
``MIN_TRADES_FOR_TRIPWIRE``, its cumulative forward profit factor must
sit inside the SAME holdout confidence interval that got it promoted to
paper trading in the first place - a pre-registered bound, not "let's
see more" if it drifts outside. Reads the holdout CI straight from
``data/processed/gap_event_walk_forward_results.json`` (the structured
output ``scripts/backtest_gap_event_walk_forward.py`` already produces,
and TRANS-0007.yaml's own ``reason``/``next_action`` prose cites) rather
than a hardcoded copy of those numbers, so a rerun of that backtest stays
the single source of truth for what "in bounds" means.

Scans every ``data/live_sessions/*/events.jsonl`` for the target
strategy's closed trades via the existing
``core.analytics.session_performance.compute_closed_trades`` - one call
PER session (not one call on the concatenated stream of every session):
that function's FIFO entry/exit matching is per-symbol, and sessions are
separate trading days with their own end-of-day square-off, so matching
across a session boundary could pair one day's still-open entry against
a LATER day's unrelated exit on the same symbol. Merges the resulting
per-session closed-trade lists by exit_timestamp afterward - the
combined stream is what a chronological forward-PF read needs regardless
of which session each trade closed in.

Exits 1 (pause-and-diagnose) on a tripwire breach, 0 otherwise (including
"not enough trades yet") - suitable for a cron/CI gate, not just a
human-read report.

Run with:
    PYTHONPATH=. python scripts/check_forward_validation_tripwire.py
    PYTHONPATH=. python scripts/check_forward_validation_tripwire.py --strategy gap_continuation --min-trades 20
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.analytics.session_performance import compute_closed_trades, compute_portfolio_metrics

LIVE_SESSIONS_DIR = Path("data/live_sessions")
HOLDOUT_RESULTS_JSON = Path("data/processed/gap_event_walk_forward_results.json")
DEFAULT_STRATEGY = "gap_continuation"
DEFAULT_MIN_TRADES = 20


def load_holdout_ci(results_json: Path = HOLDOUT_RESULTS_JSON) -> tuple[float, float, float]:
    """Returns (point_estimate, lower, upper) for the pre-registered holdout net PF."""
    holdout = json.loads(results_json.read_text(encoding="utf-8"))["holdout"]["net_pf"]
    return holdout["point_estimate"], holdout["lower"], holdout["upper"]


def collect_forward_trades(strategy_name: str, sessions_dir: Path = LIVE_SESSIONS_DIR) -> list:
    """
    One ``compute_closed_trades`` call per session directory (see module
    docstring for why not one call on the concatenated stream), filtered
    to ``strategy_name``, merged and sorted by exit_timestamp across
    every session found.
    """
    trades = []
    for session_dir in sorted(sessions_dir.glob("*")):
        events_path = session_dir / "events.jsonl"
        if not events_path.exists():
            continue
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        strategy_events = [e for e in events if e.get("strategy_name") == strategy_name]
        trades.extend(compute_closed_trades(strategy_events))
    trades.sort(key=lambda t: t.exit_timestamp)
    return trades


def check(strategy_name: str, min_trades: int) -> int:
    # Pass the module globals through explicitly rather than relying on
    # collect_forward_trades/load_holdout_ci's own default parameters -
    # those defaults are bound once at function-definition time, so a
    # caller (e.g. a test) reassigning LIVE_SESSIONS_DIR/
    # HOLDOUT_RESULTS_JSON afterward would silently have no effect on them.
    point_estimate, lower, upper = load_holdout_ci(HOLDOUT_RESULTS_JSON)
    trades = collect_forward_trades(strategy_name, LIVE_SESSIONS_DIR)

    if len(trades) < min_trades:
        print(f"{strategy_name}: {len(trades)}/{min_trades} forward trades so far - tripwire not yet evaluable.")
        return 0

    metrics = compute_portfolio_metrics(trades)
    forward_pf = metrics.profit_factor

    print(f"{strategy_name}: n={metrics.trade_count} forward trades, PF={forward_pf}, "
          f"holdout CI=[{lower:.3f},{upper:.3f}] (point estimate {point_estimate:.3f})")

    if forward_pf is None:
        print("  Forward PF not computable yet (no losing trades on record) - cannot evaluate the tripwire either way.")
        return 0

    if forward_pf < lower or forward_pf > upper:
        print(f"  TRIPWIRE BREACHED: forward PF {forward_pf:.3f} is outside the pre-registered holdout CI. "
              f"Pause and diagnose - do not wait for more trades before investigating.")
        return 1

    print("  Within the pre-registered holdout CI.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY)
    parser.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    args = parser.parse_args()
    sys.exit(check(args.strategy, args.min_trades))


if __name__ == "__main__":
    main()

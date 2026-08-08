"""
Capital ladder gate (Track 6 of docs/roadmap.md): evidence-gated, not
calendar-gated. Reports each tier's criteria as MET / NOT MET / UNKNOWN
against real repository state - never assumes a criterion passes just
because it wasn't checked.

10k -> 50k requires:
  (a) Track 1's forward metrics clearing their pre-registered bar over
      >= N trades (reuses scripts.check_forward_validation_tripwire's own
      trade-collection and holdout-CI logic - "cleared the bar" means at
      least N forward trades exist AND the forward PF sits inside the
      holdout CI, not merely that N trades happened).
  (b) zero unhandled criticals from "Track 0" over the same window -
      UNKNOWN here, not fabricated: this roadmap references a "Track 0"
      this repository has no other record of (no doc, no script, no
      registry entry name-matches it) - report as UNKNOWN and require a
      human to confirm, rather than silently treating an unverifiable
      criterion as satisfied.

50k -> 250k requires:
  (a) a second active strategy whose own research_registry_id resolves
      to decision: pass (reuses
      tests.integration.test_portfolio_governance's own validated_pass
      check - the "Validated" a second strategy needs is exactly what
      that gate already verifies for the first).
  (b) that second strategy demonstrated low correlation with the first,
      out of forward trade returns - UNKNOWN until a second strategy and
      real forward history for both exist to correlate.
  (c) core.portfolio.optimizer.compute_optimized_weights actually wired
      into a live call site (apps/paper_trader/main.py or
      apps/backtester/run_portfolio_backtest.py passing weights=), not
      merely existing as an unused module - checked directly against
      those files' source, not assumed from the module's own presence.

Run with:
    PYTHONPATH=. python scripts/check_capital_ladder_gate.py
"""
from __future__ import annotations

import re
from pathlib import Path

from scripts.check_forward_validation_tripwire import (
    DEFAULT_MIN_TRADES,
    DEFAULT_STRATEGY,
    collect_forward_trades,
    load_holdout_ci,
)
from tests.integration.test_portfolio_governance import active_strategy_paths, validated_pass

_OPTIMIZER_CALL_SITES = [
    Path("apps/paper_trader/main.py"),
    Path("apps/backtester/run_portfolio_backtest.py"),
]
_WEIGHTS_KWARG_PATTERN = re.compile(r"build_portfolio_allocation_plans\([^)]*weights\s*=")


def check_forward_metrics_cleared(strategy_name: str = DEFAULT_STRATEGY, min_trades: int = DEFAULT_MIN_TRADES) -> tuple[bool, str]:
    from core.analytics.session_performance import compute_portfolio_metrics

    trades = collect_forward_trades(strategy_name)
    if len(trades) < min_trades:
        return False, f"{len(trades)}/{min_trades} forward trades so far - bar not yet cleared (need more trades, not necessarily a failure)"

    metrics = compute_portfolio_metrics(trades)
    if metrics.profit_factor is None:
        return False, "forward PF not computable yet (no losing trades on record)"

    _point_estimate, lower, upper = load_holdout_ci()
    if metrics.profit_factor < lower or metrics.profit_factor > upper:
        return False, f"forward PF {metrics.profit_factor:.3f} outside holdout CI [{lower:.3f},{upper:.3f}] - tripwire breached"

    return True, f"n={metrics.trade_count}, forward PF={metrics.profit_factor:.3f} inside holdout CI [{lower:.3f},{upper:.3f}]"


def check_second_validated_strategy() -> tuple[bool, str]:
    results = [(path, *validated_pass(path)) for path in active_strategy_paths()]
    passing = [path for path, ok, _reason in results if ok]
    if len(passing) >= 2:
        return True, f"{len(passing)} active strategies are Validated/Pass: {', '.join(str(p) for p in passing)}"
    return False, f"only {len(passing)}/{len(results)} active strategies are Validated/Pass"


def check_optimizer_wired_in() -> tuple[bool, str]:
    wired_sites = [
        str(path) for path in _OPTIMIZER_CALL_SITES
        if path.exists() and _WEIGHTS_KWARG_PATTERN.search(path.read_text(encoding="utf-8"))
    ]
    if wired_sites:
        return True, f"weights= passed at: {', '.join(wired_sites)}"
    return False, f"no call site among {', '.join(str(p) for p in _OPTIMIZER_CALL_SITES)} passes weights= - still using default 1/N"


def main() -> None:
    print("=== 10k -> 50k ===")
    cleared, reason = check_forward_metrics_cleared()
    print(f"  [{'MET' if cleared else 'NOT MET'}] Track 1 forward metrics cleared the pre-registered bar: {reason}")
    print("  [UNKNOWN] zero unhandled criticals from \"Track 0\" over the same window - "
          "this repository has no record of what \"Track 0\" refers to; confirm with a human before promoting.")

    print("\n=== 50k -> 250k ===")
    two_validated, reason = check_second_validated_strategy()
    print(f"  [{'MET' if two_validated else 'NOT MET'}] second independently-validated strategy active: {reason}")
    print("  [UNKNOWN] that second strategy is low-correlation with the first - "
          "needs real forward return history for both to compute; none exists yet.")
    optimizer_wired, reason = check_optimizer_wired_in()
    print(f"  [{'MET' if optimizer_wired else 'NOT MET'}] Track 2 portfolio optimizer wired into allocation.py: {reason}")


if __name__ == "__main__":
    main()

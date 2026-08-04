#!/usr/bin/env python3
"""
ONE-TIME validation of the relative_volume_pullback hybrid's best
dev-set configuration against data/processed/nse_5min_holdout.csv (the
newer 6-month half) - never touched during scripts/tune_hybrid_strategy.py's
iteration. Run once; do not re-tune against this result.

    PYTHONPATH=. python scripts/validate_hybrid_holdout.py
"""

from __future__ import annotations

from core.allocations.daily_mandate import load_daily_mandate
from core.backtest.day_runner import run_multi_day_backtest
from core.backtest.engine import load_bars_from_csv
from core.backtest.report import build_performance_report
from core.market_data.candidate_profile import CandidateProfile
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.portfolio.exit_models import ExitPolicyConfig, parse_time_of_day
from core.risk.simple_intraday import SimpleIntradayRiskManager
import core.strategy.relative_volume_pullback  # noqa: F401

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
DEV_CSV = "data/processed/nse_5min_dev.csv"
HOLDOUT_CSV = "data/processed/nse_5min_holdout.csv"
STRATEGY_NAME = "relative_volume_pullback"
SLIPPAGE_BPS = 5.0

# Best dev-set result from scripts/tune_hybrid_strategy.py + the
# both-screens follow-up: relvol (1.5x/10d) + liquidity/uptrend combined,
# volume-confirmed pullback entry, 1.0% stop / 1.5% trailing stop.
BEST_PROFILE = CandidateProfile(
    require_relative_volume=True,
    relative_volume_multiplier=1.5,
    relative_volume_trailing_days=10,
    require_liquidity_and_uptrend=True,
    min_avg_volume=50_000,
    trend_lookback_bars=20,
)
BEST_PARAMS = {
    "trend_lookback_bars": 20,
    "pullback_lookback_bars": 5,
    "min_pullback_pct": 0.3,
    "volume_lookback_bars": 10,
    "min_volume_multiplier": 1.3,
}


def _run(csv_path: str, allocation_plan, exit_policy):
    bars = load_bars_from_csv(csv_path)
    symbols = sorted({bar.symbol for bar in bars})
    day_results = run_multi_day_backtest(
        bars=bars,
        symbols=symbols,
        allocation_plan=allocation_plan,
        strategy_name=STRATEGY_NAME,
        strategy_params=BEST_PARAMS,
        exit_policy=exit_policy,
        risk_manager=SimpleIntradayRiskManager(),
        slippage_bps=SLIPPAGE_BPS,
        candidate_profile=BEST_PROFILE,
    )
    return build_performance_report(day_results)


def _fmt(report) -> str:
    pf = f"{report.profit_factor:.2f}" if report.profit_factor is not None else "n/a"
    return (
        f"trades={report.total_trades:4d}  win_rate={report.win_rate*100:5.1f}%  "
        f"net_pnl={report.total_realized_pnl:9.2f}  profit_factor={pf:>5}  costs={report.total_costs:8.2f}"
    )


def main() -> None:
    mandate = load_daily_mandate(MANDATE_PATH)
    allocation_plan = build_portfolio_allocation_plan(mandate)
    square_off_time = (
        parse_time_of_day(mandate.risk_limits.force_square_off_at)
        if mandate.risk_limits.force_square_off_at
        else None
    )
    exit_policy = ExitPolicyConfig(
        stop_loss_pct=1.0, target_pct=None, square_off_time=square_off_time, trailing_stop_pct=1.5
    )

    dev_report = _run(DEV_CSV, allocation_plan, exit_policy)
    holdout_report = _run(HOLDOUT_CSV, allocation_plan, exit_policy)

    print("dev (iterated on)    :", _fmt(dev_report))
    print("holdout (first look) :", _fmt(holdout_report))
    held_up = holdout_report.total_realized_pnl > 0
    print()
    print("HOLDS UP out-of-sample" if held_up else "DOES NOT hold up out-of-sample - dev result does not generalize")


if __name__ == "__main__":
    main()

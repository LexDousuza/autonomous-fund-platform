#!/usr/bin/env python3
"""
Iterates the new relative_volume_pullback hybrid strategy ONLY against
data/processed/nse_5min_dev.csv (the older 6-month half) - a small grid
over the trailing-stop distance and the volume-confirmation multiplier,
the two knobs this hybrid actually introduces beyond plain pullback.py.
data/processed/nse_5min_holdout.csv (the newer 6-month half) is not
touched by this script at all - see scripts/validate_hybrid_holdout.py
for the one-time validation run against it.

    PYTHONPATH=. python scripts/tune_hybrid_strategy.py
"""

from __future__ import annotations

from dataclasses import dataclass

from core.allocations.daily_mandate import load_daily_mandate
from core.backtest.day_runner import run_multi_day_backtest
from core.backtest.engine import load_bars_from_csv
from core.backtest.report import BacktestPerformanceReport, build_performance_report
from core.market_data.candidate_profile import CandidateProfile
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.portfolio.exit_models import ExitPolicyConfig, parse_time_of_day
from core.risk.simple_intraday import SimpleIntradayRiskManager
import core.strategy.relative_volume_pullback  # noqa: F401

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
DEV_CSV = "data/processed/nse_5min_dev.csv"
STRATEGY_NAME = "relative_volume_pullback"
SLIPPAGE_BPS = 5.0
MIN_TRADES_TO_TRUST = 10

CANDIDATE_PROFILE = CandidateProfile(
    require_relative_volume=True, relative_volume_multiplier=1.5, relative_volume_trailing_days=10
)

BASE_PARAMS = {
    "trend_lookback_bars": 20,
    "pullback_lookback_bars": 5,
    "min_pullback_pct": 0.3,
    "volume_lookback_bars": 10,
}


@dataclass(slots=True)
class Candidate:
    label: str
    strategy_params: dict
    trailing_stop_pct: float | None
    stop_loss_pct: float


CANDIDATES = [
    Candidate("volume 1.3x, trail 1.0%, stop 1.0%", {**BASE_PARAMS, "min_volume_multiplier": 1.3}, 1.0, 1.0),
    Candidate("volume 1.3x, trail 1.5%, stop 1.0%", {**BASE_PARAMS, "min_volume_multiplier": 1.3}, 1.5, 1.0),
    Candidate("volume 1.3x, trail 2.0%, stop 1.0%", {**BASE_PARAMS, "min_volume_multiplier": 1.3}, 2.0, 1.0),
    Candidate("volume 1.5x, trail 1.5%, stop 1.0%", {**BASE_PARAMS, "min_volume_multiplier": 1.5}, 1.5, 1.0),
    Candidate("volume 2.0x, trail 1.5%, stop 1.0%", {**BASE_PARAMS, "min_volume_multiplier": 2.0}, 1.5, 1.0),
    Candidate("volume 1.3x, trail 1.5%, stop 1.5%", {**BASE_PARAMS, "min_volume_multiplier": 1.3}, 1.5, 1.5),
    Candidate("volume 1.3x, no trail (fixed 2% target instead)", {**BASE_PARAMS, "min_volume_multiplier": 1.3}, None, 1.0),
]


def _fmt(report: BacktestPerformanceReport) -> str:
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

    bars = load_bars_from_csv(DEV_CSV)
    symbols = sorted({bar.symbol for bar in bars})

    print(f"{'=' * 78}\n{STRATEGY_NAME.upper()} - DEV iteration ({len(CANDIDATES)} candidates)\n{'=' * 78}")

    for candidate in CANDIDATES:
        exit_policy = ExitPolicyConfig(
            stop_loss_pct=candidate.stop_loss_pct,
            target_pct=None if candidate.trailing_stop_pct is not None else 2.0,
            square_off_time=square_off_time,
            trailing_stop_pct=candidate.trailing_stop_pct,
        )
        day_results = run_multi_day_backtest(
            bars=bars,
            symbols=symbols,
            allocation_plan=allocation_plan,
            strategy_name=STRATEGY_NAME,
            strategy_params=candidate.strategy_params,
            exit_policy=exit_policy,
            risk_manager=SimpleIntradayRiskManager(),
            slippage_bps=SLIPPAGE_BPS,
            candidate_profile=CANDIDATE_PROFILE,
        )
        report = build_performance_report(day_results)
        flag = "" if report.total_trades >= MIN_TRADES_TO_TRUST else "  (too few trades to trust)"
        print(f"{candidate.label:45s} {_fmt(report)}{flag}")


if __name__ == "__main__":
    main()

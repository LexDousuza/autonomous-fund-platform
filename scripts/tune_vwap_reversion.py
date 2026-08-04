#!/usr/bin/env python3
"""
Iterates vwap_reversion ONLY against data/processed/nse_5min_dev.csv.

    PYTHONPATH=. python scripts/tune_vwap_reversion.py
"""

from __future__ import annotations

from dataclasses import dataclass

from core.allocations.daily_mandate import load_daily_mandate
from core.backtest.day_runner import run_multi_day_backtest
from core.backtest.engine import load_bars_from_csv
from core.backtest.report import BacktestPerformanceReport, build_performance_report
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.portfolio.exit_models import ExitPolicyConfig, parse_time_of_day
from core.risk.simple_intraday import SimpleIntradayRiskManager
import core.strategy.vwap_reversion  # noqa: F401

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
DEV_CSV = "data/processed/nse_5min_dev.csv"
STRATEGY_NAME = "vwap_reversion"
SLIPPAGE_BPS = 5.0
MIN_TRADES_TO_TRUST = 10


@dataclass(slots=True)
class Candidate:
    label: str
    min_deviation_pct: float
    stop_loss_pct: float
    target_pct: float | None
    trailing_stop_pct: float | None


CANDIDATES = [
    Candidate("dev 0.4%, stop 0.3%, target 0.4% (to VWAP-ish)", 0.4, 0.3, 0.4, None),
    Candidate("dev 0.4%, stop 0.4%, target 0.6%", 0.4, 0.4, 0.6, None),
    Candidate("dev 0.5%, stop 0.4%, target 0.6%", 0.5, 0.4, 0.6, None),
    Candidate("dev 0.6%, stop 0.4%, target 0.8%", 0.6, 0.4, 0.8, None),
    Candidate("dev 0.4%, stop 0.3%, trail 0.4%", 0.4, 0.3, None, 0.4),
    Candidate("dev 0.5%, stop 0.4%, trail 0.5%", 0.5, 0.4, None, 0.5),
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
            target_pct=candidate.target_pct,
            square_off_time=square_off_time,
            trailing_stop_pct=candidate.trailing_stop_pct,
        )
        day_results = run_multi_day_backtest(
            bars=bars,
            symbols=symbols,
            allocation_plan=allocation_plan,
            strategy_name=STRATEGY_NAME,
            strategy_params={"min_bars_for_vwap": 6, "min_deviation_pct": candidate.min_deviation_pct},
            exit_policy=exit_policy,
            risk_manager=SimpleIntradayRiskManager(),
            slippage_bps=SLIPPAGE_BPS,
            candidate_profile=None,
        )
        report = build_performance_report(day_results)
        flag = "" if report.total_trades >= MIN_TRADES_TO_TRUST else "  (too few trades to trust)"
        print(f"{candidate.label:45s} {_fmt(report)}{flag}")


if __name__ == "__main__":
    main()

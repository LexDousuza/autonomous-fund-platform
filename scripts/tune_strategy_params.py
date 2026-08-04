#!/usr/bin/env python3
"""
Parameter sweep over each backtestable strategy's own signal parameters
AND the candidate_profile liquidity+uptrend selection screen (see
core/backtest/day_runner.py), with train/test discipline: every
candidate is scored on data/processed/nse_1min_train.csv only, and only
the train-set winners are ever run against
data/processed/nse_1min_test.csv - once, to see whether the improvement
holds out-of-sample rather than being this specific train window's noise.

This does NOT pick a "best" candidate automatically and does NOT rewrite
any config/strategies/*.yaml - it prints both windows' numbers side by
side (plus the current production defaults for comparison) so a human
decides, consistent with the platform's own "Live deployment requires
explicit user approval" principle (docs/architecture.md). A result that
looks better on train but not on test is exactly the overfitting this
script is designed to expose, not hide.

    PYTHONPATH=. python scripts/tune_strategy_params.py
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
import core.strategy.ma_crossover  # noqa: F401
import core.strategy.momentum  # noqa: F401
import core.strategy.pullback  # noqa: F401

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
TRAIN_CSV = "data/processed/nse_1min_train.csv"
TEST_CSV = "data/processed/nse_1min_test.csv"
SLIPPAGE_BPS = 5.0
MIN_TRADES_TO_TRUST = 10  # below this, a "good" result is more likely luck than edge

NO_SCREEN = CandidateProfile()
WITH_SCREEN = CandidateProfile(require_liquidity_and_uptrend=True, min_avg_volume=100_000.0, trend_lookback_bars=10)


@dataclass(slots=True)
class Candidate:
    label: str
    strategy_params: dict
    candidate_profile: CandidateProfile


GRIDS: dict[str, list[Candidate]] = {
    "ma_crossover": [
        Candidate("default (5/20)", {"fast_bars": 5, "slow_bars": 20}, NO_SCREEN),
        Candidate("default (5/20) + screen", {"fast_bars": 5, "slow_bars": 20}, WITH_SCREEN),
        Candidate("slower (10/30)", {"fast_bars": 10, "slow_bars": 30}, NO_SCREEN),
        Candidate("slower (10/30) + screen", {"fast_bars": 10, "slow_bars": 30}, WITH_SCREEN),
        Candidate("slowest (15/40)", {"fast_bars": 15, "slow_bars": 40}, NO_SCREEN),
        Candidate("slowest (15/40) + screen", {"fast_bars": 15, "slow_bars": 40}, WITH_SCREEN),
        Candidate("mid (8/25)", {"fast_bars": 8, "slow_bars": 25}, NO_SCREEN),
        Candidate("mid (8/25) + screen", {"fast_bars": 8, "slow_bars": 25}, WITH_SCREEN),
    ],
    "momentum": [
        Candidate("default (1.0%/1.5x)", {"min_price_change_pct": 1.0, "min_volume_multiplier": 1.5}, NO_SCREEN),
        Candidate("default (1.0%/1.5x) + screen", {"min_price_change_pct": 1.0, "min_volume_multiplier": 1.5}, WITH_SCREEN),
        Candidate("looser (0.5%/1.2x)", {"min_price_change_pct": 0.5, "min_volume_multiplier": 1.2}, NO_SCREEN),
        Candidate("looser (0.5%/1.2x) + screen", {"min_price_change_pct": 0.5, "min_volume_multiplier": 1.2}, WITH_SCREEN),
        Candidate("stricter (2.0%/2.0x)", {"min_price_change_pct": 2.0, "min_volume_multiplier": 2.0}, NO_SCREEN),
        Candidate("stricter (2.0%/2.0x) + screen", {"min_price_change_pct": 2.0, "min_volume_multiplier": 2.0}, WITH_SCREEN),
        Candidate("strictest (2.5%/1.5x)", {"min_price_change_pct": 2.5, "min_volume_multiplier": 1.5}, NO_SCREEN),
        Candidate("strictest (2.5%/1.5x) + screen", {"min_price_change_pct": 2.5, "min_volume_multiplier": 1.5}, WITH_SCREEN),
    ],
    "pullback": [
        Candidate("default (0.3%/20)", {"min_pullback_pct": 0.3, "trend_lookback_bars": 20}, NO_SCREEN),
        Candidate("default (0.3%/20) + screen", {"min_pullback_pct": 0.3, "trend_lookback_bars": 20}, WITH_SCREEN),
        Candidate("bigger dip (0.6%/20)", {"min_pullback_pct": 0.6, "trend_lookback_bars": 20}, NO_SCREEN),
        Candidate("bigger dip (0.6%/20) + screen", {"min_pullback_pct": 0.6, "trend_lookback_bars": 20}, WITH_SCREEN),
        Candidate("biggest dip (1.0%/15)", {"min_pullback_pct": 1.0, "trend_lookback_bars": 15}, NO_SCREEN),
        Candidate("biggest dip (1.0%/15) + screen", {"min_pullback_pct": 1.0, "trend_lookback_bars": 15}, WITH_SCREEN),
        Candidate("mid (0.5%/25)", {"min_pullback_pct": 0.5, "trend_lookback_bars": 25}, NO_SCREEN),
        Candidate("mid (0.5%/25) + screen", {"min_pullback_pct": 0.5, "trend_lookback_bars": 25}, WITH_SCREEN),
    ],
}


def _run(strategy_name: str, bars, symbols, allocation_plan, exit_policy, candidate: Candidate) -> BacktestPerformanceReport:
    day_results = run_multi_day_backtest(
        bars=bars,
        symbols=symbols,
        allocation_plan=allocation_plan,
        strategy_name=strategy_name,
        strategy_params=candidate.strategy_params,
        exit_policy=exit_policy,
        risk_manager=SimpleIntradayRiskManager(),
        slippage_bps=SLIPPAGE_BPS,
        candidate_profile=candidate.candidate_profile,
    )
    return build_performance_report(day_results)


def _fmt(report: BacktestPerformanceReport) -> str:
    pf = f"{report.profit_factor:.2f}" if report.profit_factor is not None else "n/a"
    return (
        f"trades={report.total_trades:4d}  win_rate={report.win_rate*100:5.1f}%  "
        f"net_pnl={report.total_realized_pnl:9.2f}  profit_factor={pf:>5}  "
        f"costs={report.total_costs:8.2f}"
    )


def main() -> None:
    mandate = load_daily_mandate(MANDATE_PATH)
    allocation_plan = build_portfolio_allocation_plan(mandate)
    # Matches apps/backtester/run_backtest.py's own construction exactly -
    # without a real square_off_time, a position that never hits stop/target
    # just sits open and is silently dropped when the day's fresh session
    # ends, understating win rate (square_off exits, often near-breakeven,
    # never get counted at all).
    square_off_time = (
        parse_time_of_day(mandate.risk_limits.force_square_off_at)
        if mandate.risk_limits.force_square_off_at
        else None
    )
    # Flat pct only, matching apps/live_trader and apps/paper_trader's
    # actual (reverted) exit policy - a 14-bar ATR on 1-minute data proved
    # ~14x tighter than a flat 1.5% stop, causing near-universal premature
    # stop-outs when this script first tried it. See apps/live_trader/main.py's
    # exit_policy comment for the measurement.
    exit_policy = ExitPolicyConfig(stop_loss_pct=1.5, target_pct=2.0, square_off_time=square_off_time)

    train_bars = load_bars_from_csv(TRAIN_CSV)
    test_bars = load_bars_from_csv(TEST_CSV)
    symbols = sorted({bar.symbol for bar in train_bars})

    for strategy_name, candidates in GRIDS.items():
        print(f"\n{'=' * 70}\n{strategy_name.upper()} - TRAIN sweep ({len(candidates)} candidates)\n{'=' * 70}")

        train_reports: list[tuple[Candidate, BacktestPerformanceReport]] = []
        for candidate in candidates:
            report = _run(strategy_name, train_bars, symbols, allocation_plan, exit_policy, candidate)
            train_reports.append((candidate, report))
            print(f"{candidate.label:32s} {_fmt(report)}")

        trustworthy = [(c, r) for c, r in train_reports if r.total_trades >= MIN_TRADES_TO_TRUST]
        if not trustworthy:
            print(f"\nNo candidate reached {MIN_TRADES_TO_TRUST}+ trades on train - "
                  "every result above is too small a sample to rank meaningfully.")
            continue

        ranked = sorted(trustworthy, key=lambda pair: pair[1].total_realized_pnl, reverse=True)
        top = ranked[:2]

        print(f"\nTop {len(top)} by train net PnL (min {MIN_TRADES_TO_TRUST} trades) -> validating ONCE on held-out test:")
        for candidate, train_report in top:
            test_report = _run(strategy_name, test_bars, symbols, allocation_plan, exit_policy, candidate)
            held_up = test_report.total_realized_pnl > 0 and train_report.total_realized_pnl > 0
            print(f"\n  {candidate.label}")
            print(f"    train: {_fmt(train_report)}")
            print(f"    test:  {_fmt(test_report)}")
            print(f"    {'HOLDS UP out-of-sample' if held_up else 'DOES NOT hold up out-of-sample - treat the train result as noise, not edge'}")


if __name__ == "__main__":
    main()

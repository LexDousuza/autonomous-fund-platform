#!/usr/bin/env python3
"""
One-shot check of two mechanically-grounded hypotheses about why every
strategy/parameter combination in scripts/tune_strategy_params.py lost
money, using the SAME train/test bars - not a new parameter search, a
diagnostic.

1. Capital-per-trade vs. Groww's brokerage floor: sample_mandate.yaml's
   intraday_bucket (10,000) / 4-way split (core/portfolio/allocation.py)
   caps each trade at Rs 2,500 - below the Rs 5,000 notional where
   estimate_groww_brokerage's Rs 5 floor stops dominating over its
   headline 0.1% rate. A Rs 1,362 trade pays Rs 5 flat = 0.37% effective,
   not 0.1%. Tests whether a 10x bigger capital bucket (same daily-loss
   cap scaled the same 10x, so the comparison isn't just "allowed to lose
   more") changes cost drag and the bottom line.
2. Bar interval: 1-minute bars may be mostly noise for these signals -
   resamples the same 1-minute data to 5-minute bars (fewer, hopefully
   higher-quality trades, same total price history) and re-runs.

Only the CURRENT DEFAULT parameters are tested here (not a new sweep) -
this isolates the two variables in question rather than re-introducing
the same multi-comparison overfitting risk the rest of this session has
been careful about.

    PYTHONPATH=. python scripts/test_cost_and_interval_hypotheses.py
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime

from core.allocations.daily_mandate import load_daily_mandate
from core.backtest.day_runner import run_multi_day_backtest
from core.backtest.engine import load_bars_from_csv
from core.backtest.report import BacktestPerformanceReport, build_performance_report
from core.market_data.candidate_profile import CandidateProfile
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.portfolio.exit_models import ExitPolicyConfig, parse_time_of_day
from core.risk.simple_intraday import SimpleIntradayRiskManager
from core.strategy.base import MarketBar
import core.strategy.ma_crossover  # noqa: F401
import core.strategy.momentum  # noqa: F401
import core.strategy.pullback  # noqa: F401

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
TRAIN_CSV = "data/processed/nse_1min_train.csv"
TEST_CSV = "data/processed/nse_1min_test.csv"
SLIPPAGE_BPS = 5.0

DEFAULT_PARAMS = {
    "ma_crossover": {"fast_bars": 5, "slow_bars": 20},
    "momentum": {"min_price_change_pct": 1.0, "min_volume_multiplier": 1.5},
    "pullback": {"min_pullback_pct": 0.3, "trend_lookback_bars": 20},
}


def _fmt(report: BacktestPerformanceReport) -> str:
    pf = f"{report.profit_factor:.2f}" if report.profit_factor is not None else "n/a"
    cost_pct = (report.total_costs / abs(report.total_realized_pnl - report.total_costs) * 100) if report.total_trades else 0.0
    return (
        f"trades={report.total_trades:4d}  win_rate={report.win_rate*100:5.1f}%  "
        f"net_pnl={report.total_realized_pnl:9.2f}  profit_factor={pf:>5}  "
        f"costs={report.total_costs:8.2f} ({cost_pct:5.1f}% of gross)"
    )


def _resample_5min(bars: list[MarketBar]) -> list[MarketBar]:
    """Aggregate 1-minute bars into 5-minute bars, per symbol, in time order."""
    by_symbol: dict[str, list[MarketBar]] = defaultdict(list)
    for bar in bars:
        by_symbol[bar.symbol].append(bar)

    resampled: list[MarketBar] = []
    for symbol, symbol_bars in by_symbol.items():
        symbol_bars.sort(key=lambda b: b.timestamp)
        bucket: list[MarketBar] = []
        bucket_start: datetime | None = None
        for bar in symbol_bars:
            slot = bar.timestamp.replace(minute=(bar.timestamp.minute // 5) * 5, second=0, microsecond=0)
            if bucket_start is not None and slot != bucket_start:
                resampled.append(_merge(bucket, bucket_start))
                bucket = []
            bucket_start = slot
            bucket.append(bar)
        if bucket:
            resampled.append(_merge(bucket, bucket_start))
    return resampled


def _merge(bucket: list[MarketBar], slot: datetime) -> MarketBar:
    first = bucket[0]
    return MarketBar(
        symbol=first.symbol,
        exchange=first.exchange,
        timestamp=slot,
        open=bucket[0].open,
        high=max(b.high for b in bucket),
        low=min(b.low for b in bucket),
        close=bucket[-1].close,
        volume=sum(b.volume for b in bucket),
    )


def _run(bars, symbols, allocation_plan, exit_policy, strategy_name) -> BacktestPerformanceReport:
    day_results = run_multi_day_backtest(
        bars=bars,
        symbols=symbols,
        allocation_plan=allocation_plan,
        strategy_name=strategy_name,
        strategy_params=DEFAULT_PARAMS[strategy_name],
        exit_policy=exit_policy,
        risk_manager=SimpleIntradayRiskManager(),
        slippage_bps=SLIPPAGE_BPS,
        candidate_profile=CandidateProfile(),
    )
    return build_performance_report(day_results)


def main() -> None:
    mandate = load_daily_mandate(MANDATE_PATH)
    square_off_time = (
        parse_time_of_day(mandate.risk_limits.force_square_off_at)
        if mandate.risk_limits.force_square_off_at
        else None
    )
    exit_policy = ExitPolicyConfig(stop_loss_pct=1.5, target_pct=2.0, square_off_time=square_off_time)

    train_bars = load_bars_from_csv(TRAIN_CSV)
    test_bars = load_bars_from_csv(TEST_CSV)
    symbols = sorted({bar.symbol for bar in train_bars})

    baseline_plan = build_portfolio_allocation_plan(mandate)

    big_mandate = load_daily_mandate(MANDATE_PATH)
    big_mandate.buckets.intraday_bucket *= 10  # Rs 2,500/trade -> Rs 25,000/trade
    big_mandate.risk_limits.max_daily_loss *= 10  # scale the loss cap the same way - not "allowed to lose more", just not artificially truncating entries
    big_plan = build_portfolio_allocation_plan(big_mandate)

    train_5min = _resample_5min(train_bars)
    test_5min = _resample_5min(test_bars)

    for strategy_name in DEFAULT_PARAMS:
        print(f"\n{'=' * 78}\n{strategy_name.upper()}\n{'=' * 78}")

        for label, bars, plan in [
            ("baseline (Rs 2,500/trade, 1-min bars) - train", train_bars, baseline_plan),
            ("baseline (Rs 2,500/trade, 1-min bars) - test ", test_bars, baseline_plan),
            ("10x capital (Rs 25,000/trade, 1-min bars) - train", train_bars, big_plan),
            ("10x capital (Rs 25,000/trade, 1-min bars) - test ", test_bars, big_plan),
            ("5-min bars, Rs 2,500/trade - train", train_5min, baseline_plan),
            ("5-min bars, Rs 2,500/trade - test ", test_5min, baseline_plan),
        ]:
            report = _run(bars, symbols, plan, exit_policy, strategy_name)
            print(f"  {label:48s} {_fmt(report)}")


if __name__ == "__main__":
    main()

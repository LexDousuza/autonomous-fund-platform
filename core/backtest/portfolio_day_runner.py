"""
Multi-strategy (portfolio) multi-day backtest runner.

The N-strategy sibling of ``core.backtest.day_runner.run_multi_day_backtest``:
reuses ``ExecutionOrchestrator.run_portfolio_session`` (the same
multi-strategy coordinator paper trading uses - see
``apps/paper_trader/main.py``'s ``build_paper_portfolio_session``) instead
of ``run_session``, one trading day at a time, so a portfolio backtest
reflects the exact same symbol-exclusivity/shared-kill-switch/independent-
capital semantics a real multi-strategy paper or live session would.

Each day gets a fresh ``PaperBroker``, ``MarketDataService``, and (inside
the orchestrator) a fresh ``PositionManager``/per-strategy
``SessionRiskGuardrails`` - positions and daily counters never carry
across days, matching ``run_multi_day_backtest``'s identical behavior for
a single strategy.

Per-strategy ``candidate_profile`` screening (liquidity+uptrend and/or
relative-volume - see ``core/market_data/candidate_profile.py``) runs day
by day, exactly as ``run_multi_day_backtest`` already does for one
strategy, just independently per strategy here: each strategy narrows its
own static bucket-selected symbol list down to that day's screened subset,
using only price history strictly BEFORE the day being decided for (the
same look-ahead-free accumulation ``run_multi_day_backtest`` uses).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date

from core.backtest.day_runner import BacktestDayResult
from core.broker.paper import PaperBroker
from core.execution.models import TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.candidate_profile import CandidateProfile
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.market_data.universe_models import Instrument
from core.market_data.universe_screener import (
    LiquidityTrendScreeningCriteria,
    RelativeVolumeScreeningCriteria,
    screen_by_liquidity_and_trend,
    screen_by_relative_volume,
)
from core.portfolio.exit_models import ExitPolicyConfig
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.base import RiskManager
from core.strategy.base import MarketBar
from core.strategy.registry import StrategyRegistry
from core.strategy.registry import registry as default_strategy_registry


@dataclass(slots=True)
class StrategyBacktestSpec:
    """
    One active strategy's static inputs for a portfolio backtest - the
    N-strategy sibling of ``run_multi_day_backtest``'s flat
    ``(symbols, strategy_name, strategy_params, exit_policy,
    allocation_plan, candidate_profile)`` arguments, gathered into one
    struct per strategy since a portfolio backtest needs a list of these.

    ``symbols`` is the strategy's own static bucket-selected universe for
    the whole backtest (already capital/risk-appetite-screened against its
    own apportioned ``allocation_plan`` - see
    ``apps/backtester/run_portfolio_backtest.py``); ``candidate_profile``'s
    day-by-day screen (if any) only ever narrows this list further, never
    adds to it.
    """

    strategy_name: str
    symbols: list[str]
    allocation_plan: PortfolioAllocationPlan
    strategy_params: dict | None = None
    exit_policy: ExitPolicyConfig | None = None
    candidate_profile: CandidateProfile | None = None


def _screen_day_symbols(
    base_symbols: list[str],
    profile: CandidateProfile | None,
    price_history_so_far: dict[str, list[MarketBar]],
) -> list[str]:
    """Narrow ``base_symbols`` via ``profile``'s day-by-day screens, if any - see module docstring."""
    day_symbols = base_symbols

    if profile is not None and profile.require_liquidity_and_uptrend:
        instruments = [Instrument(symbol=s, exchange="NSE", segment="CASH") for s in day_symbols]
        screening = screen_by_liquidity_and_trend(
            instruments,
            price_history_so_far,
            LiquidityTrendScreeningCriteria(
                min_avg_daily_volume=profile.min_avg_volume,
                trend_lookback_bars=profile.trend_lookback_bars,
            ),
        )
        day_symbols = sorted(instrument.symbol for instrument in screening.selected)

    if profile is not None and profile.require_relative_volume:
        instruments = [Instrument(symbol=s, exchange="NSE", segment="CASH") for s in day_symbols]
        relative_volume_screening = screen_by_relative_volume(
            instruments,
            price_history_so_far,
            RelativeVolumeScreeningCriteria(
                relative_volume_multiplier=profile.relative_volume_multiplier,
                trailing_days=profile.relative_volume_trailing_days,
            ),
        )
        day_symbols = sorted(instrument.symbol for instrument in relative_volume_screening.selected)

    return day_symbols


def run_multi_day_portfolio_backtest(
    bars: list[MarketBar],
    strategies: list[StrategyBacktestSpec],
    risk_manager: RiskManager,
    strategy_registry: StrategyRegistry = default_strategy_registry,
    slippage_bps: float = 0.0,
) -> dict[str, list[BacktestDayResult]]:
    """
    Group ``bars`` by calendar date and run one fresh
    ``ExecutionOrchestrator.run_portfolio_session`` per trading day, in
    chronological order, across every strategy in ``strategies``
    concurrently.

    Returns one ``list[BacktestDayResult]`` per strategy name, all the
    same length and aligned to the same trading_date sequence (a strategy
    with nothing to trade on a given day still gets an entry for it,
    matching ``core.backtest.report.build_combined_day_results``'s
    alignment requirement) - feed the result straight to
    ``build_performance_report`` per strategy, or through
    ``build_combined_day_results`` first for one portfolio-level report.
    """
    if not strategies:
        raise ValueError("strategies must not be empty")
    strategy_names = [spec.strategy_name for spec in strategies]
    if len(set(strategy_names)) != len(strategy_names):
        raise ValueError("strategies must have unique strategy_name values")

    bars_by_day: dict[date, list[MarketBar]] = defaultdict(list)
    for bar in bars:
        bars_by_day[bar.timestamp.date()].append(bar)

    # Accumulated strictly BEFORE the day currently being processed,
    # shared across strategies (it's just observed market history, not
    # anything strategy-specific) - see module docstring.
    price_history_so_far: dict[str, list[MarketBar]] = defaultdict(list)

    results: dict[str, list[BacktestDayResult]] = {name: [] for name in strategy_names}
    for trading_date in sorted(bars_by_day):
        day_bars_all = bars_by_day[trading_date]

        day_symbols_by_strategy = {
            spec.strategy_name: _screen_day_symbols(spec.symbols, spec.candidate_profile, price_history_so_far)
            for spec in strategies
        }

        all_day_symbols = {s for symbols in day_symbols_by_strategy.values() for s in symbols}
        day_bars = [bar for bar in day_bars_all if bar.symbol in all_day_symbols]

        market_data_service = MarketDataService()
        market_data_service.register_feed("portfolio_backtest_day", MemoryMarketDataFeed(bars=day_bars))

        orchestrator = ExecutionOrchestrator(
            market_data_service=market_data_service,
            risk_manager=risk_manager,
            broker=PaperBroker(slippage_bps=slippage_bps),
            strategy_registry=strategy_registry,
        )

        session_configs = [
            TradingSessionConfig(
                trading_date=trading_date,
                strategy_name=spec.strategy_name,
                strategy_params=spec.strategy_params or {},
                execution_mode="backtest",
                symbols=day_symbols_by_strategy[spec.strategy_name],
                portfolio_allocation_plan=replace(spec.allocation_plan, trading_date=trading_date),
                exit_policy=spec.exit_policy,
            )
            for spec in strategies
        ]

        day_results = orchestrator.run_portfolio_session(session_configs)
        for name in strategy_names:
            results[name].append(BacktestDayResult(trading_date=trading_date, session_result=day_results[name]))

        # Feed screening for the NEXT day, not this one - every symbol's
        # full-day bars, not just today's screened-in subset, so a symbol
        # excluded today can still become eligible tomorrow (matches
        # run_multi_day_backtest's identical accumulation).
        for bar in day_bars_all:
            price_history_so_far[bar.symbol].append(bar)

    return results

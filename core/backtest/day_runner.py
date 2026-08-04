"""
Multi-day backtest runner.

Reuses ``ExecutionOrchestrator`` - the exact same engine paper and live
trading use (position sizing, exit rules, session risk guardrails,
portfolio exposure limits) - one trading day at a time, so backtest
results reflect the real execution pipeline rather than a simplified
stand-in. This replaces the earlier V1 ``SimpleBacktestEngine``, which
had its own separate, much simpler rules (fixed quantity, no exit
engine, no risk guardrails) that didn't predict how the strategy would
actually behave in paper/live trading.

Each day gets a fresh ``PaperBroker``, ``MarketDataService``, and (inside
the orchestrator) a fresh ``PositionManager``/``SessionRiskGuardrails`` -
positions and daily counters never carry across days, matching how
intraday trading actually resets every day.

``candidate_profile`` (optional) applies the same liquidity+uptrend
screen ``apps/live_trader/candidate_selection.py`` already runs for live
sessions (see ``core/market_data/universe_screener.py``'s
``screen_by_liquidity_and_trend``) - previously never exercised in a
backtest at all, so a strategy's own declared candidate_profile (in its
``config/strategies/*.yaml``) was silently a no-op here. Each day's
screen sees only bars from *before* that day (accumulated as the loop
progresses), never that day's own bars or later ones - a real screen
using only information available at the time, not a look-ahead repeat of
the same bug already fixed in ``apps/backtester/run_backtest.py``'s
bucket selection.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date

from core.broker.paper import PaperBroker
from core.execution.models import ExecutionSessionResult, TradingSessionConfig
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
class BacktestDayResult:
    trading_date: date
    session_result: ExecutionSessionResult


def run_multi_day_backtest(
    bars: list[MarketBar],
    symbols: list[str],
    allocation_plan: PortfolioAllocationPlan,
    strategy_name: str,
    risk_manager: RiskManager,
    strategy_params: dict | None = None,
    exit_policy: ExitPolicyConfig | None = None,
    strategy_registry: StrategyRegistry = default_strategy_registry,
    slippage_bps: float = 0.0,
    candidate_profile: CandidateProfile | None = None,
) -> list[BacktestDayResult]:
    """
    Group ``bars`` by calendar date and run one fresh
    ``ExecutionOrchestrator`` session per trading day, in chronological
    order.

    ``allocation_plan``'s capital/risk numbers are reused every day (only
    ``trading_date`` is overridden per day) - a fixed daily capital
    allocation, matching how a real mandate is authored per trading day.
    V1 scope: this evaluates strategy/system *mechanics* across many
    days, not compounding equity-curve capital day over day.

    ``slippage_bps`` is forwarded to each day's fresh ``PaperBroker`` -
    0.0 (exact requested-price fills) by default, matching this
    function's historical behavior. ``apps/backtester/run_backtest.py``
    passes a non-zero default so backtest performance isn't inflated by
    assuming perfect fills (see ``core/broker/paper.py``).
    """
    bars_by_day: dict[date, list[MarketBar]] = defaultdict(list)
    for bar in bars:
        bars_by_day[bar.timestamp.date()].append(bar)

    # Accumulated strictly BEFORE the day currently being processed - see
    # this module's docstring on why candidate_profile's screen must
    # never see the day it's deciding for.
    price_history_so_far: dict[str, list[MarketBar]] = defaultdict(list)

    results: list[BacktestDayResult] = []
    for trading_date in sorted(bars_by_day):
        day_bars = bars_by_day[trading_date]
        day_symbols = symbols

        if candidate_profile is not None and candidate_profile.require_liquidity_and_uptrend:
            instruments = [Instrument(symbol=s, exchange="NSE", segment="CASH") for s in symbols]
            screening = screen_by_liquidity_and_trend(
                instruments,
                price_history_so_far,
                LiquidityTrendScreeningCriteria(
                    min_avg_daily_volume=candidate_profile.min_avg_volume,
                    trend_lookback_bars=candidate_profile.trend_lookback_bars,
                ),
            )
            day_symbols = sorted(instrument.symbol for instrument in screening.selected)
            day_bars = [bar for bar in day_bars if bar.symbol in day_symbols]

        if candidate_profile is not None and candidate_profile.require_relative_volume:
            # Independent of (and composable with) the liquidity+uptrend
            # screen above - "was this symbol unusually active recently"
            # is a different question from "is it generally liquid". Runs
            # against whatever day_symbols the liquidity+uptrend screen
            # already narrowed to (both screens apply together when both
            # are enabled), same look-ahead-free price_history_so_far.
            instruments = [Instrument(symbol=s, exchange="NSE", segment="CASH") for s in day_symbols]
            relative_volume_screening = screen_by_relative_volume(
                instruments,
                price_history_so_far,
                RelativeVolumeScreeningCriteria(
                    relative_volume_multiplier=candidate_profile.relative_volume_multiplier,
                    trailing_days=candidate_profile.relative_volume_trailing_days,
                ),
            )
            day_symbols = sorted(instrument.symbol for instrument in relative_volume_screening.selected)
            day_bars = [bar for bar in day_bars if bar.symbol in day_symbols]

        market_data_service = MarketDataService()
        market_data_service.register_feed("backtest_day", MemoryMarketDataFeed(bars=day_bars))

        orchestrator = ExecutionOrchestrator(
            market_data_service=market_data_service,
            risk_manager=risk_manager,
            broker=PaperBroker(slippage_bps=slippage_bps),
            strategy_registry=strategy_registry,
        )

        session_config = TradingSessionConfig(
            trading_date=trading_date,
            strategy_name=strategy_name,
            strategy_params=strategy_params or {},
            execution_mode="backtest",
            symbols=day_symbols,
            portfolio_allocation_plan=replace(allocation_plan, trading_date=trading_date),
            exit_policy=exit_policy,
        )

        session_result = orchestrator.run_session(session_config)
        results.append(BacktestDayResult(trading_date=trading_date, session_result=session_result))

        # Feed screening for the NEXT day, not this one - every symbol's
        # full-day bars, not just today's screened-in subset, so a symbol
        # excluded today can still become eligible tomorrow.
        for bar in bars_by_day[trading_date]:
            price_history_so_far[bar.symbol].append(bar)

    return results

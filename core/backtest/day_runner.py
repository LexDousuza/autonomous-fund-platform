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
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date

from core.broker.paper import PaperBroker
from core.execution.models import ExecutionSessionResult, TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
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
    """
    bars_by_day: dict[date, list[MarketBar]] = defaultdict(list)
    for bar in bars:
        bars_by_day[bar.timestamp.date()].append(bar)

    results: list[BacktestDayResult] = []
    for trading_date in sorted(bars_by_day):
        day_bars = bars_by_day[trading_date]

        market_data_service = MarketDataService()
        market_data_service.register_feed("backtest_day", MemoryMarketDataFeed(bars=day_bars))

        orchestrator = ExecutionOrchestrator(
            market_data_service=market_data_service,
            risk_manager=risk_manager,
            broker=PaperBroker(),
            strategy_registry=strategy_registry,
        )

        session_config = TradingSessionConfig(
            trading_date=trading_date,
            strategy_name=strategy_name,
            strategy_params=strategy_params or {},
            execution_mode="backtest",
            symbols=symbols,
            portfolio_allocation_plan=replace(allocation_plan, trading_date=trading_date),
            exit_policy=exit_policy,
        )

        session_result = orchestrator.run_session(session_config)
        results.append(BacktestDayResult(trading_date=trading_date, session_result=session_result))

    return results

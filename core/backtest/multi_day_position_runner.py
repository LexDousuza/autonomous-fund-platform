"""
Multi-day, position-CARRYING backtest runner.

The sibling of ``core.backtest.day_runner.run_multi_day_backtest`` for a
strategy whose own economics require holding a position across the
simulated-day boundary (e.g. ``vertical_credit_spread`` selling premium
against a multi-day expiry to capture theta decay) - that function
deliberately rebuilds a fresh ``PaperBroker``/``PositionManager`` every
day, "positions and daily counters never carry across days," which is
exactly right for every intraday-only strategy and exactly wrong for
this one.

Only the position itself needs to survive the day boundary. Daily
counters (``entries_taken``, ``max_trades_per_day``, the daily-loss
latch) still reset every simulated day, unchanged - they are legitimately
per-day even for a multi-day *strategy*, since a fresh trading day really
does bring a fresh daily budget. That's why this reuses one
``PaperBroker`` and one ``PositionManager`` across the whole loop (via
``ExecutionOrchestrator.run_session``'s optional ``position_manager=``
parameter) but still builds a fresh ``ExecutionOrchestrator``/
``SessionRiskGuardrails`` every day, exactly like ``run_multi_day_backtest``
does - no new mechanism needed for that half at all.

``strategy_params`` may include an ``option_chain_provider`` (as every
options strategy already expects). If that provider exposes
``set_current_date``/``get_price_by_tradingsymbol`` (see
``core.market_data.historical_option_chain.HistoricalOptionChainProvider``
- duck-typed, exactly like ``SignalPipeline`` already duck-types
``seed_synthetic_price_path`` on a market data feed), this runner calls
``set_current_date`` once per simulated day and synthesizes one bar per
currently-open non-underlying leg (e.g. an already-open option contract)
from that day's real settle price, so ``PositionManager.evaluate_exits``
has something to evaluate a carried leg against even though that leg
never appears in the caller-supplied ``bars`` (which only ever cover the
underlying). A provider without these methods (any ordinary,
single-day options provider) works exactly as it does in
``run_multi_day_backtest`` - carried legs simply won't be requested from
one, since a strategy that never sets ``allow_naked_writing`` etc. never
opens one in the first place.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date

from core.backtest.day_runner import BacktestDayResult
from core.broker.paper import PaperBroker
from core.execution.models import TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.portfolio.exit_models import ExitPolicyConfig
from core.portfolio.models import PortfolioAllocationPlan
from core.portfolio.position_manager import PositionManager
from core.risk.base import RiskManager
from core.strategy.base import MarketBar
from core.strategy.registry import StrategyRegistry
from core.strategy.registry import registry as default_strategy_registry

__all__ = ["run_multi_day_backtest_with_carry"]


def _synthetic_bars_for_carried_legs(
    position_manager: PositionManager,
    underlying_symbols: set[str],
    option_chain_provider: object | None,
    trading_date: date,
    timestamp,
) -> list[MarketBar]:
    """
    One flat-price bar per currently-open leg that isn't one of this
    day's underlying symbols (i.e. an already-open option contract),
    priced from the provider's real historical settle price for today -
    see module docstring. Silently skips a leg the provider has no price
    for on this date (contract expired, or a genuine data gap) rather
    than guessing a price.

    ponytail: a silent skip means a leg with a data gap simply never
    gets exit-evaluated that day rather than surfacing the gap anywhere;
    fine for an exploratory backtest, upgrade to a returned/logged count
    of skipped days if this ever needs to be trusted unattended.
    """
    get_price = getattr(option_chain_provider, "get_price_by_tradingsymbol", None)
    if get_price is None:
        return []

    bars = []
    for tracked in position_manager.open_positions().values():
        if tracked.symbol in underlying_symbols:
            continue
        price = get_price(tracked.symbol, trading_date)
        if price is None:
            continue
        bars.append(
            MarketBar(
                symbol=tracked.symbol, exchange=tracked.exchange, timestamp=timestamp,
                open=price, high=price, low=price, close=price, volume=0.0,
            )
        )
    return bars


def run_multi_day_backtest_with_carry(
    bars: list[MarketBar],
    symbols: list[str],
    allocation_plan: PortfolioAllocationPlan,
    strategy_name: str,
    risk_manager: RiskManager,
    strategy_params: dict | None = None,
    exit_policy: ExitPolicyConfig | None = None,
    strategy_registry: StrategyRegistry = default_strategy_registry,
    slippage_bps: float = 0.0,
) -> list[BacktestDayResult]:
    """
    Group ``bars`` (the underlying's own bars - one or more per day) by
    calendar date and run one fresh ``ExecutionOrchestrator`` session per
    trading day, in chronological order, sharing one ``PaperBroker`` and
    one ``PositionManager`` across every day so a position opened on one
    day is still tracked - and exit-evaluated - on later days.

    ``allocation_plan``'s capital/risk numbers are reused every day (only
    ``trading_date`` is overridden per day), matching
    ``run_multi_day_backtest``'s identical convention.
    """
    bars_by_day: dict[date, list[MarketBar]] = defaultdict(list)
    for bar in bars:
        bars_by_day[bar.timestamp.date()].append(bar)

    strategy_params = strategy_params or {}
    option_chain_provider = strategy_params.get("option_chain_provider")
    set_current_date = getattr(option_chain_provider, "set_current_date", None)

    broker = PaperBroker(slippage_bps=slippage_bps)
    position_manager = PositionManager()
    # Shared across every day, same as position_manager/broker above - a
    # lookback-dependent strategy (e.g. a realized-vol estimate over N
    # prior closes) needs its bar history to survive the day boundary
    # too, not just its open position. See run_session's own docstring
    # for why this is a second, independent carry-forward seam from
    # position_manager, not covered by it.
    history_by_symbol: dict[str, list[MarketBar]] = {}

    results: list[BacktestDayResult] = []
    for trading_date in sorted(bars_by_day):
        if set_current_date is not None:
            set_current_date(trading_date)

        day_bars = list(bars_by_day[trading_date])
        day_bars.extend(
            _synthetic_bars_for_carried_legs(
                position_manager, set(symbols), option_chain_provider, trading_date, day_bars[0].timestamp
            )
        )

        market_data_service = MarketDataService()
        market_data_service.register_feed("multi_day_carry_day", MemoryMarketDataFeed(bars=day_bars))
        # A fresh MarketDataService every day means yesterday's
        # SignalPipeline._track_symbol_for_future_bars subscription
        # (core/execution/signal_pipeline.py) never carries forward -
        # re-subscribe every currently-open leg explicitly, or
        # MemoryMarketDataFeed.stream()'s subscription filter would
        # silently drop the synthetic bars just injected for them above.
        for tracked in position_manager.open_positions().values():
            market_data_service.subscribe(tracked.symbol, exchange=tracked.exchange)

        orchestrator = ExecutionOrchestrator(
            market_data_service=market_data_service,
            risk_manager=risk_manager,
            broker=broker,
            strategy_registry=strategy_registry,
        )

        session_config = TradingSessionConfig(
            trading_date=trading_date,
            strategy_name=strategy_name,
            strategy_params=strategy_params,
            execution_mode="backtest",
            symbols=symbols,
            portfolio_allocation_plan=replace(allocation_plan, trading_date=trading_date),
            exit_policy=exit_policy,
            # Same paper-mode margin-simulation convention every other
            # paper/estimated-margin call site in this codebase already
            # uses (apps/paper_trader/main.py, apps/live_trader/main.py's
            # paper branch) - without this, OptionsMarginRiskManager
            # rejects every naked-write open outright regardless of
            # whether a real margin calculator is wired, since
            # available_margin would otherwise stay None all backtest long.
            available_margin=allocation_plan.intraday_capital,
        )

        session_result = orchestrator.run_session(
            session_config, position_manager=position_manager, history_by_symbol=history_by_symbol
        )
        results.append(BacktestDayResult(trading_date=trading_date, session_result=session_result))

    return results

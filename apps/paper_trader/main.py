from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from apps.live_trader.config import (
    GrowwEnvCredentials,
    KiteEnvCredentials,
    load_groww_credentials_from_env,
    load_kite_credentials_from_env,
)
from apps.live_trader.main import (
    ACTIVE_PORTFOLIO_PATH,
    APPROVED_CAPITAL_PATH,
    build_exit_policy_for_strategy,
    check_holding_period_is_supported,
    load_active_portfolio,
    load_strategy_config,
)
from apps.paper_trader.config import PaperTraderConfig, load_paper_trader_config_from_env
from core.allocations.approved_capital import load_approved_capital
from core.allocations.daily_mandate import DailyMandate, load_daily_mandate
from core.audit.file_sink import FileAuditSink
from core.audit.models import SessionMetadata, SessionSummary
from core.audit.sink import AuditSink
from core.broker.base import Broker
from core.broker.estimated_margin import EstimatedSpanMarginCalculator
from core.broker.factory import build_broker
from core.broker.factory_models import BrokerConfig
from core.execution.models import ExecutionSessionResult, TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.bucket_selector import (
    BucketSelectionCriteria,
    BucketSelectionResult,
    CapitalRiskBucketSelector,
)
from core.market_data.candidate_profile import CandidateProfile
from core.market_data.factory import build_market_data_feed
from core.market_data.factory_models import MarketDataConfig
from core.market_data.fo_iv_screen import resolve_iv_by_symbol
from core.market_data.kite_option_chain import KiteOptionChainProvider
from core.market_data.kite_transport import (
    KiteMarketDataCredentials,
    KiteMarketDataTransport,
    KiteMarketDataTransportConfig,
)
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.market_data.universe_loader import load_instrument_universe
from core.market_data.universe_models import Instrument
from core.market_data.universe_selector import TradingUniverseSelector, UniverseSelectionCriteria
from core.portfolio.allocation import build_portfolio_allocation_plan, build_portfolio_allocation_plans
from core.portfolio.exit_models import ExitPolicyConfig, parse_time_of_day
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.options_margin import OptionsMarginRiskManager
from core.risk.simple_intraday import (
    DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES,
    IntradayRiskSettings,
    SimpleIntradayRiskManager,
)
from core.strategy.base import MarketBar
import core.strategy.credit_spread  # noqa: F401  (registers the strategy)
import core.strategy.options_mispricing  # noqa: F401  (registers the strategy)
import core.strategy.simple_breakout_momentum  # noqa: F401  (registers the strategy)

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
UNIVERSE_PATH = "config/instruments/india_cash_universe.yaml"
INDEX_UNIVERSE_PATH = "config/instruments/india_index_universe.yaml"


# (open_delta, high_delta, low_delta, close_delta, volume) per bar,
# relative to each instrument's base_price: 5 quiet consolidation bars
# (stable, highs capped around +5) establishing a real reference high,
# then a breakout bar (closes above that reference high, volume spike),
# then a bar that rallies well past the exit engine's profit target.
# 5 quiet bars matches simple_breakout_momentum's default
# breakout_lookback_bars=5 (see that module's docstring) - fewer bars of
# history than the lookback needs and the strategy never fires at all.
_DEMO_BAR_SHAPE: list[tuple[float, float, float, float, float]] = [
    (-2.0, 3.0, -8.0, -1.0, 90000),
    (-1.0, 4.0, -7.0, 0.0, 92000),
    (-2.0, 5.0, -8.0, 1.0, 91000),
    (-1.0, 3.0, -6.0, -1.0, 93000),
    (0.0, 4.0, -7.0, 0.0, 90000),
    (0.0, 25.0, -3.0, 20.0, 160000),  # breakout: closes above the ~+5 reference high
    (22.0, 225.0, 15.0, 220.0, 120000),  # rallies well past the profit target
]


def build_session_bars(instruments: list[Instrument]) -> list[MarketBar]:
    """
    Build a small deterministic, interleaved multi-symbol intraday bar
    stream, standing in for a live feed.

    For each instrument this replays the same shape (see
    ``_DEMO_BAR_SHAPE``: 5 quiet consolidation bars, a breakout bar, then
    a bar that rallies past the exit engine's profit target) at a
    distinct price level per symbol, so the scenario is clearly
    multi-symbol while staying deterministic. Bars are interleaved by
    time offset (all symbols' bar 0, then all symbols' bar 1, ...) to
    mimic a live feed streaming several symbols roughly simultaneously.

    Called with every *candidate* instrument (before bucket selection
    narrows them down), since bucket selection itself needs price history
    to work from - see ``build_paper_session``.
    """
    base_time = datetime.now().replace(second=0, microsecond=0)
    bars: list[MarketBar] = []

    for offset, (open_delta, high_delta, low_delta, close_delta, volume) in enumerate(_DEMO_BAR_SHAPE):
        timestamp = base_time + timedelta(minutes=offset)
        for index, instrument in enumerate(instruments):
            base_price = 500.0 + index * 300.0

            bars.append(
                MarketBar(
                    symbol=instrument.symbol,
                    exchange=instrument.exchange,
                    timestamp=timestamp,
                    open=base_price + open_delta,
                    high=base_price + high_delta,
                    low=base_price + low_delta,
                    close=base_price + close_delta,
                    volume=volume,
                )
            )

    return bars


@dataclass(slots=True)
class PaperSessionContext:
    """Everything needed to run a paper trading session."""

    paper_config: PaperTraderConfig
    mandate: DailyMandate
    allocation_plan: PortfolioAllocationPlan
    candidate_instruments: list[Instrument]
    bucket_result: BucketSelectionResult
    session_symbols: list[str]
    broker_config: BrokerConfig
    broker: Broker
    market_data_config: MarketDataConfig
    orchestrator: ExecutionOrchestrator
    session_config: TradingSessionConfig


def _apply_config_overrides(mandate: DailyMandate, config: PaperTraderConfig) -> None:
    """
    Apply this run's capital/risk-appetite overrides on top of the loaded
    mandate, then re-validate. The mandate *file* is never touched - this
    only affects the in-memory object for this invocation, matching
    ``PaperTraderConfig``'s "mandate default, env override" contract.
    """
    if config.intraday_capital_override is not None:
        mandate.buckets.intraday_bucket = config.intraday_capital_override
    if config.risk_appetite_override is not None:
        mandate.risk_appetite = config.risk_appetite_override
    mandate.validate()


def _build_candidate_market_data_config(
    config: PaperTraderConfig,
    candidate_instruments: list[Instrument],
    groww_credentials: GrowwEnvCredentials | None,
    kite_credentials: KiteEnvCredentials | None = None,
) -> MarketDataConfig:
    """
    Build the ``MarketDataConfig`` covering every *candidate* instrument
    (not yet the bucket) - bucket selection needs each candidate's price
    history before it can decide which ones make the cut. When
    ``CandidateProfile.fixed_universe_segment`` is set, ``candidate_instruments``
    is already the small fixed set (e.g. NIFTY/BANKNIFTY) rather than the
    full cash-equity universe - this function doesn't need to know or
    care, it just fetches history for whatever list it's given.

    ``"groww"``/``"kite"`` fetch real historical candles for the
    configured date range/interval. ``"memory"`` (the default) generates
    the same synthetic demo bars the paper trader has always used, just
    for the full candidate set instead of a pre-filtered one - bucket
    selection runs identically either way, since it only cares about
    price history, not where it came from.
    """
    if config.market_data_provider == "groww":
        return MarketDataConfig(
            provider="groww",
            mode="historical",
            metadata={
                "api_key": groww_credentials.api_key,
                "secret": groww_credentials.secret,
                "instruments": [
                    {"symbol": instrument.symbol, "exchange": instrument.exchange}
                    for instrument in candidate_instruments
                ],
                "from_date": config.historical_from_date,
                "to_date": config.historical_to_date,
                "interval": config.historical_interval,
            },
        )

    if config.market_data_provider == "kite":
        return MarketDataConfig(
            provider="kite",
            mode="historical",
            metadata={
                "api_key": kite_credentials.api_key,
                "access_token": kite_credentials.access_token,
                "instruments": [
                    {"symbol": instrument.symbol, "exchange": instrument.exchange}
                    for instrument in candidate_instruments
                ],
                "from_date": config.historical_from_date,
                "to_date": config.historical_to_date,
                "interval": config.historical_interval,
            },
        )

    return MarketDataConfig(
        provider="memory",
        mode="paper",
        metadata={"bars": build_session_bars(candidate_instruments)},
    )


@dataclass(slots=True)
class _StrategyBucketSelection:
    candidate_instruments: list[Instrument]
    bucket_result: BucketSelectionResult
    session_symbols: list[str]
    market_data_config: MarketDataConfig
    market_data_feed: object


def _select_strategy_bucket(
    paper_config: PaperTraderConfig,
    universe_path: str,
    mandate: DailyMandate,
    allocation_plan: PortfolioAllocationPlan,
    candidate_profile: CandidateProfile,
    groww_credentials: GrowwEnvCredentials | None,
    kite_credentials: KiteEnvCredentials | None,
) -> _StrategyBucketSelection:
    """
    Candidate-instrument selection + bucket selection for ONE strategy -
    shared by ``build_paper_session`` (one strategy, one call) and
    ``build_paper_portfolio_session`` (N strategies, one call each,
    each against its own apportioned ``allocation_plan``). Extracted
    verbatim from ``build_paper_session``'s steps 3-5 - see that
    function's inline comments (before this extraction) for the full
    reasoning behind each branch.
    """
    if candidate_profile.fixed_universe_segment is not None:
        universe = load_instrument_universe(INDEX_UNIVERSE_PATH)
        candidate_instruments = TradingUniverseSelector().select(
            universe, UniverseSelectionCriteria(segment=candidate_profile.fixed_universe_segment)
        )
    else:
        universe = load_instrument_universe(universe_path)
        candidate_instruments = TradingUniverseSelector().select(
            universe, UniverseSelectionCriteria(exchange="NSE")
        )

    market_data_config = _build_candidate_market_data_config(
        paper_config, candidate_instruments, groww_credentials, kite_credentials
    )
    market_data_feed = build_market_data_feed(market_data_config)
    price_history = {
        instrument.symbol: market_data_feed.get_history(instrument.symbol)
        for instrument in candidate_instruments
    }

    if candidate_profile.fixed_universe_segment is not None:
        bucket_result = BucketSelectionResult(selected=candidate_instruments)
    else:
        bucket_criteria = BucketSelectionCriteria(
            risk_appetite=mandate.risk_appetite,
            max_capital_per_trade=allocation_plan.max_capital_per_trade,
            max_bucket_size=allocation_plan.max_open_positions,
            rank_by_volatility=candidate_profile.rank_by_volatility,
        )
        iv_by_symbol = resolve_iv_by_symbol(
            candidate_instruments,
            price_history,
            paper_config.market_data_provider,
            groww_credentials,
            kite_credentials,
        )
        bucket_result = CapitalRiskBucketSelector().select(
            candidate_instruments, price_history, bucket_criteria, iv_by_symbol
        )

    return _StrategyBucketSelection(
        candidate_instruments=candidate_instruments,
        bucket_result=bucket_result,
        session_symbols=[instrument.symbol for instrument in bucket_result.selected],
        market_data_config=market_data_config,
        market_data_feed=market_data_feed,
    )


def build_paper_session(
    mandate_path: str = MANDATE_PATH,
    universe_path: str = UNIVERSE_PATH,
    config: PaperTraderConfig | None = None,
) -> PaperSessionContext:
    """
    Build everything needed for a paper trading session, without running
    it.

    ``config`` defaults to ``load_paper_trader_config_from_env()`` - tests
    can inject a specific ``PaperTraderConfig`` directly. Kept separate
    from ``run_paper_session()`` so tests can construct and inspect a
    session without streaming any bars.
    """
    paper_config = config if config is not None else load_paper_trader_config_from_env()

    # 1) load today's mandate, apply this run's capital/risk-appetite
    #    overrides (if any), and derive the day's allocation plan. Also
    #    load which strategy this session runs (see load_strategy_config,
    #    reused from apps.live_trader.main) - previously hardcoded to
    #    simple_breakout_momentum with empty params; the default
    #    strategy_config_path reproduces that exactly (see
    #    PaperTraderConfig's docstring).
    mandate = load_daily_mandate(mandate_path)
    _apply_config_overrides(mandate, paper_config)
    allocation_plan = build_portfolio_allocation_plan(mandate)
    strategy_name, strategy_params, candidate_profile = load_strategy_config(
        paper_config.strategy_config_path
    )
    check_holding_period_is_supported(strategy_name)

    # 2) load Groww/Kite credentials only if actually requested, ahead of
    #    everything below that might need them (candidate market data,
    #    the IV screen, and the options-chain-provider injection).
    groww_credentials: GrowwEnvCredentials | None = None
    if paper_config.requires_groww_credentials:
        groww_credentials = load_groww_credentials_from_env()

    kite_credentials: KiteEnvCredentials | None = None
    if paper_config.requires_kite_credentials:
        kite_credentials = load_kite_credentials_from_env()
        # Injected unconditionally whenever Kite credentials are loaded -
        # harmless (an unused dict key) for a cash-equity strategy, the
        # only way an options-based strategy's option_chain_provider
        # param (see core.strategy.options_mispricing's docstring) is
        # ever populated for a paper session. Mirrors
        # apps/live_trader/main.py's identical injection.
        strategy_params["option_chain_provider"] = KiteOptionChainProvider(
            KiteMarketDataTransport(
                KiteMarketDataTransportConfig(
                    credentials=KiteMarketDataCredentials(
                        api_key=kite_credentials.api_key, access_token=kite_credentials.access_token
                    )
                )
            ),
            risk_free_rate=float(strategy_params.get("risk_free_rate", 0.07)),
        )

    # 3-5) candidate-instrument selection + bucket selection - see
    #    _select_strategy_bucket's docstring; extracted so
    #    build_paper_portfolio_session can call it once per active
    #    strategy without duplicating this logic.
    selection = _select_strategy_bucket(
        paper_config, universe_path, mandate, allocation_plan, candidate_profile,
        groww_credentials, kite_credentials,
    )
    candidate_instruments = selection.candidate_instruments
    bucket_result = selection.bucket_result
    session_symbols = selection.session_symbols
    market_data_config = selection.market_data_config
    market_data_feed = selection.market_data_feed

    # 6) build the actual SESSION feed from only the bucket's already-
    #    fetched bars (no second network round-trip for the groww
    #    provider - get_history() just reads back what step 3 already
    #    cached). This is deliberately a *fresh*, bucket-only feed rather
    #    than reusing the candidate feed directly: every MarketDataFeed
    #    here treats "no symbols subscribed" as "stream everything, no
    #    filter" (see e.g. MemoryMarketDataFeed.stream()'s docstring), so
    #    an empty bucket subscribing to nothing on the candidate feed
    #    would silently stream every candidate instead of nothing -
    #    exactly backwards from what bucket selection is for. Scoping the
    #    feed itself to the bucket closes that gap regardless of whether
    #    the bucket is empty, partial, or full.
    session_bars = sorted(
        (bar for symbol in session_symbols for bar in market_data_feed.get_history(symbol)),
        key=lambda bar: (bar.timestamp, bar.symbol),
    )

    # 7) initialize dependencies. The broker is built through the broker
    #    factory rather than instantiated directly.
    broker_config = BrokerConfig(provider="paper", mode="paper")
    broker = build_broker(broker_config)
    # A naive, paper-only margin simulation (see EstimatedSpanMarginCalculator's
    # docstring) so a naked-write-open here actually exercises
    # OptionsMarginRiskManager's fail-closed gate - matching the
    # real gating a Kite/Groww live session applies - rather than being
    # approved purely on capital-sizing grounds as before.
    risk_manager = OptionsMarginRiskManager(
        SimpleIntradayRiskManager(
            IntradayRiskSettings(
                min_profit_to_brokerage_ratio_by_strategy=DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES
            )
        ),
        margin_calculator=EstimatedSpanMarginCalculator(),
    )

    market_data_service = MarketDataService()
    market_data_service.register_feed("paper_session", MemoryMarketDataFeed(bars=session_bars))

    orchestrator = ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=risk_manager,
        broker=broker,
    )

    # 8) describe the session (including V1 intraday exit rules) and hand
    #    it to the orchestrator. Order quantity is not set here:
    #    ExecutionOrchestrator derives it per signal via
    #    PositionSizingEngine, from allocation_plan.max_capital_per_trade
    #    and the triggering bar's price. square_off_time comes from the
    #    mandate's force_square_off_at - previously parsed but never
    #    actually enforced anywhere, a real gap for an intraday-only
    #    platform (see core/portfolio/exit_models.py's parse_time_of_day).
    square_off_time = (
        parse_time_of_day(mandate.risk_limits.force_square_off_at)
        if mandate.risk_limits.force_square_off_at
        else None
    )
    # See apps/live_trader/main.py's build_exit_policy_for_strategy: most
    # strategies get the platform's flat 1.5%/2.0% default (deliberately
    # NOT ATR-based - a prior pass measured that as actively harmful on
    # real 1-minute bars), but a strategy whose own design requires
    # square_off_time alone gets exactly that instead.
    exit_policy = build_exit_policy_for_strategy(strategy_name, square_off_time)

    session_config = TradingSessionConfig(
        trading_date=allocation_plan.trading_date,
        strategy_name=strategy_name,
        strategy_params=strategy_params,
        execution_mode=allocation_plan.execution_mode,
        symbols=session_symbols,
        portfolio_allocation_plan=allocation_plan,
        exit_policy=exit_policy,
        # Simulated available margin for the estimator above to compare
        # against - reuses the session's own intraday capital budget
        # rather than inventing a separate paper-margin config value,
        # since paper trading has no real margin concept to draw a more
        # precise figure from anyway.
        available_margin=allocation_plan.intraday_capital,
    )

    return PaperSessionContext(
        paper_config=paper_config,
        mandate=mandate,
        allocation_plan=allocation_plan,
        candidate_instruments=candidate_instruments,
        bucket_result=bucket_result,
        session_symbols=session_symbols,
        broker_config=broker_config,
        broker=broker,
        market_data_config=market_data_config,
        orchestrator=orchestrator,
        session_config=session_config,
    )


def print_startup_banner(ctx: PaperSessionContext) -> None:
    plan = ctx.allocation_plan
    print("=== PAPER TRADER START ===")
    print(f"Trading date: {plan.trading_date}")
    print(f"Intraday capital: {plan.intraday_capital}")
    print(f"Risk appetite: {ctx.mandate.risk_appetite}")
    print(f"Market data: {ctx.market_data_config.provider}/{ctx.market_data_config.mode}")
    print(f"Execution mode: {plan.execution_mode}")
    print(f"Broker: {ctx.broker_config.provider}/{ctx.broker_config.mode}")
    print()

    print("=== BUCKET SELECTION ===")
    print(f"Candidates considered ({len(ctx.candidate_instruments)}): "
          f"{', '.join(i.symbol for i in ctx.candidate_instruments)}")
    print(f"Bucket selected ({len(ctx.session_symbols)}): {', '.join(ctx.session_symbols) or 'none'}")
    if ctx.bucket_result.excluded:
        print(f"Excluded ({len(ctx.bucket_result.excluded)}):")
        for exclusion in ctx.bucket_result.excluded:
            print(f"  -> {exclusion.exchange}:{exclusion.symbol} reason={exclusion.reason}")
    print()


def print_session_summary(result: ExecutionSessionResult) -> None:
    """Print the multi-symbol session summary, mirroring the live trader's report."""
    print("=== SESSION SUMMARY ===")
    print(f"Strategy: {result.strategy_name}")
    print(f"Bars processed: {result.processed_bars}")
    print(f"Signals generated: {result.generated_signals}")
    print(f"Symbols with signals: {', '.join(sorted(result.symbols_with_signals)) or 'none'}")
    print(f"Signals approved: {result.approved_signals}")
    print(f"Signals rejected: {result.rejected_signals}")
    print(f"  - skipped due to session guardrails: {result.skipped_due_to_session_guardrails}")
    print(f"  - skipped due to position limits: {result.skipped_due_to_position_limits}")
    print(f"Duplicate orders blocked: {result.duplicate_orders_blocked}")
    print()

    placed_symbols = sorted({order.symbol for order in result.placed_orders})
    print(f"Orders placed: {len(result.placed_orders)}")
    print(f"Symbols with placed orders: {', '.join(placed_symbols) or 'none'}")
    for order in result.placed_orders:
        print(
            f"  -> {order.exchange}:{order.symbol} id={order.order_id} "
            f"status={order.status.value} qty={order.quantity} avg_price={order.average_price}"
        )
    print()

    print(f"Exits triggered: {len(result.exit_decisions)}")
    for decision, exit_order in zip(result.exit_decisions, result.exit_orders):
        print(
            f"  -> {decision.exchange}:{decision.symbol} reason={decision.reason} "
            f"id={exit_order.order_id} avg_price={exit_order.average_price}"
        )
    print()

    print("=== FINAL POSITIONS BY SYMBOL ===")
    if not result.final_positions:
        print("No positions")
    else:
        for p in sorted(result.final_positions, key=lambda position: position.symbol):
            print(
                f"{p.exchange}:{p.symbol} | qty={p.quantity} | "
                f"avg={p.average_price} | product={p.product_type.value}"
            )
    print()

    state = result.final_session_risk_state
    print("=== SESSION RISK STATE ===")
    print(
        f"Entries taken: {state.entries_taken} | realized_pnl: {state.realized_pnl} | "
        f"entry_blocked: {state.entry_blocked} | block_reason: {state.block_reason}"
    )

    print()
    print("=== PAPER TRADER END ===")


def _generate_session_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{uuid4().hex[:8]}"


def run_paper_session(audit_sink: AuditSink | None = None) -> ExecutionSessionResult:
    """Build a paper session, print the pre-flight banner, run it, and print the summary.

    Persists via ``audit_sink`` (default ``FileAuditSink``, same as live_trader) so a
    dashboard can read the session's positions/trades from data/live_sessions/.
    """
    sink = audit_sink if audit_sink is not None else FileAuditSink()
    session_id = _generate_session_id()
    started_at = datetime.now(UTC)

    try:
        ctx = build_paper_session()
    except Exception as exc:
        sink.start_session(SessionMetadata(session_id=session_id, app_name="paper_trader", started_at=started_at))
        sink.record_failure(exc)
        raise

    sink.start_session(
        SessionMetadata(
            session_id=session_id,
            app_name="paper_trader",
            started_at=started_at,
            trading_date=ctx.allocation_plan.trading_date,
            strategy_name=ctx.session_config.strategy_name,
            broker_provider=ctx.broker_config.provider,
            broker_mode=ctx.broker_config.mode,
            market_data_provider=ctx.market_data_config.provider,
            market_data_mode=ctx.market_data_config.mode,
            subscribed_symbols=list(ctx.session_symbols),
            allocation_plan=ctx.allocation_plan,
            exit_policy=ctx.session_config.exit_policy,
        )
    )
    ctx.orchestrator.audit_sink = sink
    print_startup_banner(ctx)

    try:
        result = ctx.orchestrator.run_session(ctx.session_config)
        print_session_summary(result)
        sink.finalize_session(
            SessionSummary(
                strategy_name=result.strategy_name,
                trading_date=result.trading_date,
                processed_bars=result.processed_bars,
                generated_signals=result.generated_signals,
                symbols_with_signals=sorted(result.symbols_with_signals),
                approved_signals=result.approved_signals,
                rejected_signals=result.rejected_signals,
                skipped_due_to_session_guardrails=result.skipped_due_to_session_guardrails,
                skipped_due_to_position_limits=result.skipped_due_to_position_limits,
                placed_orders=result.placed_orders,
                exit_decisions=result.exit_decisions,
                exit_orders=result.exit_orders,
                final_positions=result.final_positions,
                final_session_risk_state=result.final_session_risk_state,
                succeeded=True,
                duplicate_orders_blocked=result.duplicate_orders_blocked,
            )
        )
        return result
    except Exception as exc:
        sink.record_failure(exc)
        raise


@dataclass(slots=True)
class PaperPortfolioSessionContext:
    """Everything needed to run a multi-strategy autonomous paper trading session."""

    paper_config: PaperTraderConfig
    mandate: DailyMandate
    approved_capital: float
    allocation_plans: dict[str, PortfolioAllocationPlan]
    selections: dict[str, _StrategyBucketSelection]
    broker_config: BrokerConfig
    broker: Broker
    orchestrator: ExecutionOrchestrator
    session_configs: list[TradingSessionConfig]


def build_paper_portfolio_session(
    mandate_path: str = MANDATE_PATH,
    universe_path: str = UNIVERSE_PATH,
    active_portfolio_path: str = ACTIVE_PORTFOLIO_PATH,
    approved_capital_path: str = APPROVED_CAPITAL_PATH,
    config: PaperTraderConfig | None = None,
) -> PaperPortfolioSessionContext:
    """
    Build everything needed for a multi-strategy autonomous paper trading
    session, without running it - the N-strategy sibling of
    ``build_paper_session``.

    The human's only lever is ``approved_capital_path``'s total (see
    ``core.allocations.approved_capital``) - it overrides
    ``mandate.buckets.intraday_bucket`` via the exact same in-memory-only
    override mechanism ``build_paper_session`` already uses for its
    debug-only ``PAPER_INTRADAY_CAPITAL`` env var, just always applied
    here rather than optional, since it's portfolio mode's one required
    approval gate rather than a debug convenience. Which strategies are
    active is ``active_portfolio_path``'s plain, git-tracked list - not a
    second approval ceremony.
    """
    paper_config = config if config is not None else load_paper_trader_config_from_env()

    mandate = load_daily_mandate(mandate_path)
    approved_capital = load_approved_capital(approved_capital_path)
    mandate.buckets.intraday_bucket = approved_capital
    if paper_config.risk_appetite_override is not None:
        mandate.risk_appetite = paper_config.risk_appetite_override
    mandate.validate()

    strategy_paths = load_active_portfolio(active_portfolio_path)
    strategy_configs = [load_strategy_config(path) for path in strategy_paths]
    strategy_names = [name for name, _, _ in strategy_configs]
    if len(set(strategy_names)) != len(strategy_names):
        raise ValueError(f"{active_portfolio_path} lists the same strategy more than once")
    for name in strategy_names:
        check_holding_period_is_supported(name)

    allocation_plans = build_portfolio_allocation_plans(mandate, strategy_names)

    groww_credentials: GrowwEnvCredentials | None = None
    if paper_config.requires_groww_credentials:
        groww_credentials = load_groww_credentials_from_env()

    kite_credentials: KiteEnvCredentials | None = None
    if paper_config.requires_kite_credentials:
        kite_credentials = load_kite_credentials_from_env()

    selections: dict[str, _StrategyBucketSelection] = {}
    for strategy_name, strategy_params, candidate_profile in strategy_configs:
        if kite_credentials is not None:
            # Unconditional whenever Kite credentials are loaded, exactly
            # as build_paper_session does - harmless for a cash-equity
            # strategy, the only way an options strategy's
            # option_chain_provider param gets populated for paper mode.
            strategy_params["option_chain_provider"] = KiteOptionChainProvider(
                KiteMarketDataTransport(
                    KiteMarketDataTransportConfig(
                        credentials=KiteMarketDataCredentials(
                            api_key=kite_credentials.api_key, access_token=kite_credentials.access_token
                        )
                    )
                ),
                risk_free_rate=float(strategy_params.get("risk_free_rate", 0.07)),
            )
        selections[strategy_name] = _select_strategy_bucket(
            paper_config,
            universe_path,
            mandate,
            allocation_plans[strategy_name],
            candidate_profile,
            groww_credentials,
            kite_credentials,
        )

    # One canonical bar stream for the whole portfolio: the first
    # strategy (in active_portfolio.yaml order) to select a given symbol
    # supplies its bars for everyone - avoids two strategies that
    # independently select the same symbol seeing two inconsistent,
    # independently-fetched/generated copies (most concretely an issue
    # for the "memory" provider's synthetic demo bars, which price an
    # instrument by its position in whatever candidate list generated it;
    # for a real Kite/Groww historical fetch this is simply the same real
    # data either way).
    session_bars_by_key: dict[tuple[str, str], list[MarketBar]] = {}
    for strategy_name in strategy_names:
        selection = selections[strategy_name]
        for instrument in selection.bucket_result.selected:
            key = (instrument.exchange, instrument.symbol)
            if key not in session_bars_by_key:
                session_bars_by_key[key] = selection.market_data_feed.get_history(instrument.symbol)

    session_bars = sorted(
        (bar for bars in session_bars_by_key.values() for bar in bars),
        key=lambda bar: (bar.timestamp, bar.symbol),
    )

    broker_config = BrokerConfig(provider="paper", mode="paper")
    broker = build_broker(broker_config)
    risk_manager = OptionsMarginRiskManager(
        SimpleIntradayRiskManager(
            IntradayRiskSettings(
                min_profit_to_brokerage_ratio_by_strategy=DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES
            )
        ),
        margin_calculator=EstimatedSpanMarginCalculator(),
    )

    market_data_service = MarketDataService()
    market_data_service.register_feed("paper_portfolio_session", MemoryMarketDataFeed(bars=session_bars))

    orchestrator = ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=risk_manager,
        broker=broker,
    )

    square_off_time = (
        parse_time_of_day(mandate.risk_limits.force_square_off_at)
        if mandate.risk_limits.force_square_off_at
        else None
    )

    # Per-strategy, not one shared policy for the whole portfolio - see
    # build_exit_policy_for_strategy's docstring: a strategy whose own
    # design requires square_off_time alone must actually get that,
    # regardless of which other strategies are active alongside it.
    session_configs = [
        TradingSessionConfig(
            trading_date=allocation_plans[strategy_name].trading_date,
            strategy_name=strategy_name,
            strategy_params=strategy_params,
            execution_mode=allocation_plans[strategy_name].execution_mode,
            symbols=selections[strategy_name].session_symbols,
            portfolio_allocation_plan=allocation_plans[strategy_name],
            exit_policy=build_exit_policy_for_strategy(strategy_name, square_off_time),
            available_margin=allocation_plans[strategy_name].intraday_capital,
        )
        for strategy_name, strategy_params, _ in strategy_configs
    ]

    return PaperPortfolioSessionContext(
        paper_config=paper_config,
        mandate=mandate,
        approved_capital=approved_capital,
        allocation_plans=allocation_plans,
        selections=selections,
        broker_config=broker_config,
        broker=broker,
        orchestrator=orchestrator,
        session_configs=session_configs,
    )


def print_portfolio_startup_banner(ctx: PaperPortfolioSessionContext) -> None:
    print("=== PAPER PORTFOLIO TRADER START ===")
    print(f"Trading date: {ctx.mandate.trading_date}")
    print(f"Approved capital: {ctx.approved_capital}")
    print(f"Risk appetite: {ctx.mandate.risk_appetite}")
    print(f"Broker: {ctx.broker_config.provider}/{ctx.broker_config.mode}")
    active_names = [config.strategy_name for config in ctx.session_configs]
    print(f"Active strategies ({len(active_names)}): {', '.join(active_names)}")
    print()

    for config in ctx.session_configs:
        plan = config.portfolio_allocation_plan
        selection = ctx.selections[config.strategy_name]
        print(f"--- {config.strategy_name} ---")
        print(f"  Intraday capital: {plan.intraday_capital}")
        print(
            f"  Bucket selected ({len(selection.session_symbols)}): "
            f"{', '.join(selection.session_symbols) or 'none'}"
        )
    print()


def run_paper_portfolio_session(
    audit_sink: AuditSink | None = None,
    mandate_path: str = MANDATE_PATH,
    universe_path: str = UNIVERSE_PATH,
    active_portfolio_path: str = ACTIVE_PORTFOLIO_PATH,
    approved_capital_path: str = APPROVED_CAPITAL_PATH,
    config: PaperTraderConfig | None = None,
) -> dict[str, ExecutionSessionResult]:
    """
    Build a multi-strategy autonomous paper session, print each active
    strategy's pre-flight banner and summary, run it, and persist one
    combined audit session - the N-strategy sibling of ``run_paper_session``.
    """
    sink = audit_sink if audit_sink is not None else FileAuditSink()
    session_id = _generate_session_id()
    started_at = datetime.now(UTC)

    try:
        ctx = build_paper_portfolio_session(
            mandate_path=mandate_path,
            universe_path=universe_path,
            active_portfolio_path=active_portfolio_path,
            approved_capital_path=approved_capital_path,
            config=config,
        )
    except Exception as exc:
        sink.start_session(SessionMetadata(session_id=session_id, app_name="paper_trader", started_at=started_at))
        sink.record_failure(exc)
        raise

    strategy_names = [config.strategy_name for config in ctx.session_configs]
    sink.start_session(
        SessionMetadata(
            session_id=session_id,
            app_name="paper_trader",
            started_at=started_at,
            trading_date=ctx.mandate.trading_date,
            strategy_name=", ".join(strategy_names),
            strategy_names=strategy_names,
            broker_provider=ctx.broker_config.provider,
            broker_mode=ctx.broker_config.mode,
            subscribed_symbols=sorted({s for config in ctx.session_configs for s in config.symbols}),
        )
    )
    ctx.orchestrator.audit_sink = sink
    print_portfolio_startup_banner(ctx)

    try:
        results = ctx.orchestrator.run_portfolio_session(ctx.session_configs)
        for strategy_name in strategy_names:
            print_session_summary(results[strategy_name])
        sink.finalize_session(
            SessionSummary(
                strategy_name=", ".join(strategy_names),
                strategy_names=strategy_names,
                trading_date=ctx.mandate.trading_date,
                processed_bars=sum(r.processed_bars for r in results.values()),
                generated_signals=sum(r.generated_signals for r in results.values()),
                symbols_with_signals=sorted({s for r in results.values() for s in r.symbols_with_signals}),
                approved_signals=sum(r.approved_signals for r in results.values()),
                rejected_signals=sum(r.rejected_signals for r in results.values()),
                skipped_due_to_session_guardrails=sum(
                    r.skipped_due_to_session_guardrails for r in results.values()
                ),
                skipped_due_to_position_limits=sum(r.skipped_due_to_position_limits for r in results.values()),
                placed_orders=[o for r in results.values() for o in r.placed_orders],
                exit_decisions=[d for r in results.values() for d in r.exit_decisions],
                exit_orders=[o for r in results.values() for o in r.exit_orders],
                final_positions=ctx.broker.list_positions(),
                # No single SessionRiskState makes sense across N
                # independent per-strategy guardrails - see each
                # result's own final_session_risk_state, or the
                # per-strategy-attributed events.jsonl, for the real
                # per-strategy detail.
                final_session_risk_state=None,
                succeeded=True,
                duplicate_orders_blocked=sum(r.duplicate_orders_blocked for r in results.values()),
            )
        )
        return results
    except Exception as exc:
        sink.record_failure(exc)
        raise


def main() -> None:
    # Dispatch, not run_paper_session itself: today's single-strategy
    # entrypoint stays completely untouched (no new dependency on
    # active_portfolio.yaml) so every existing direct caller/test of it
    # is unaffected - only the CLI entrypoint gains the new decision.
    active_strategies = load_active_portfolio(ACTIVE_PORTFOLIO_PATH)
    if len(active_strategies) > 1:
        run_paper_portfolio_session()
    else:
        run_paper_session()


if __name__ == "__main__":
    main()

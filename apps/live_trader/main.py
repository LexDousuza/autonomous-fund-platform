from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from apps.live_trader.aws_secrets import load_secrets_into_env
from apps.live_trader.config import (
    GrowwEnvCredentials,
    KiteEnvCredentials,
    LiveTraderConfig,
    load_groww_credentials_from_env,
    load_kite_credentials_from_env,
    load_live_trader_config_from_env,
)
from core.allocations.daily_mandate import load_daily_mandate
from core.audit.file_sink import FileAuditSink, find_unclean_prior_session
from core.audit.models import SessionMetadata, SessionSummary
from core.audit.sink import AuditSink
from core.broker.base import Broker
from core.broker.factory import build_broker
from core.broker.factory_models import BrokerConfig
from core.execution.models import ExecutionSessionResult, TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.execution.reconciliation import ReconciliationResult, check_broker_reconciliation
from core.market_data.bucket_selector import (
    BucketSelectionCriteria,
    BucketSelectionResult,
    CapitalRiskBucketSelector,
)
from core.market_data.factory import build_market_data_feed
from core.market_data.factory_models import MarketDataConfig
from core.market_data.service import MarketDataService
from core.market_data.universe_loader import load_instrument_universe
from core.market_data.universe_models import Instrument, InstrumentUniverse
from core.market_data.universe_selector import TradingUniverseSelector, UniverseSelectionCriteria
from core.notifications.alert_sink import AlertSink, ConsoleAlertSink
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.portfolio.exit_models import ExitPolicyConfig, parse_time_of_day
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.simple_intraday import SimpleIntradayRiskManager
from core.strategy.base import MarketBar
import core.strategy.simple_breakout_momentum  # noqa: F401  (registers the strategy)

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
UNIVERSE_PATH = "config/instruments/india_cash_universe.yaml"


class LiveTransportNotReadyError(RuntimeError):
    """
    Raised when the live trader is fully wired (mandate, allocation,
    universe, broker factory, orchestrator) but the selected broker's
    real transport is not implemented yet, so no live order could
    actually be placed.
    """


class UncleanPriorSessionError(RuntimeError):
    """
    Raised when the previous session never reached a terminal status and
    the operator hasn't explicitly acknowledged that with
    ``LIVE_ALLOW_UNCLEAN_PRIOR_SESSION``.

    Distinct from ``UnreconciledPositionsError``: that check asks the
    *broker* whether positions are unexpectedly open; this one asks the
    *local process history* whether the last run ever finished at all -
    a session can crash flat (no open positions) and still be a crash
    worth a human looking at before the next one assumes a clean slate.
    See ``core.audit.file_sink.find_unclean_prior_session``.
    """


class UnreconciledPositionsError(RuntimeError):
    """
    Raised when the broker reports open positions before this session
    has placed anything, and the operator hasn't explicitly acknowledged
    that with ``LIVE_ALLOW_UNRECONCILED_POSITIONS``.

    Distinct from ordinary config validation errors, like
    ``LiveTradingNotConfirmedError``: this isn't about what the operator
    configured, it's about what the broker's real account already looks
    like - trading on top of an unaccounted-for book would mean position
    sizing and exposure guardrails are working from wrong assumptions
    from the very first bar.
    """


@dataclass(slots=True)
class LiveSessionContext:
    """Everything needed to run (or refuse to run) a live trading session."""

    live_config: LiveTraderConfig
    allocation_plan: PortfolioAllocationPlan
    universe: InstrumentUniverse
    candidate_instruments: list[Instrument]
    bucket_result: BucketSelectionResult
    session_instruments: list[Instrument]
    session_symbols: list[str]
    broker_config: BrokerConfig
    broker: Broker
    reconciliation: ReconciliationResult
    market_data_config: MarketDataConfig
    orchestrator: ExecutionOrchestrator
    session_config: TradingSessionConfig


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

    This is the same demo shape used by the paper trader (see
    ``_DEMO_BAR_SHAPE``: 5 quiet consolidation bars, a breakout bar, then
    a bar that rallies past the exit engine's profit target, per
    instrument at a distinct price level). Used only for the safe
    local/dev market-data path (``LIVE_MARKET_DATA_PROVIDER=memory``, the
    default) - see ``_build_market_data_config``. The real live path
    (``LIVE_MARKET_DATA_PROVIDER=groww``) uses ``GrowwLiveMarketDataFeed``
    instead and never touches these bars.
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


def _build_candidate_market_data_config(
    live_config: LiveTraderConfig,
    candidate_instruments: list[Instrument],
    groww_credentials: GrowwEnvCredentials | None,
    kite_credentials: KiteEnvCredentials | None,
) -> MarketDataConfig:
    """
    Build the ``MarketDataConfig`` covering every *candidate* instrument,
    used only to fetch the price history bucket selection needs - mirrors
    ``apps/paper_trader/main.py``'s identical helper.

    ``"groww"``/``"kite"`` fetch a short window of real historical
    candles (``live_config.historical_from_date``/``to_date``/``interval``)
    purely for this affordability/volatility read - the session itself
    still streams live quotes separately (see
    ``_build_market_data_config``). ``"memory"`` reuses the same
    synthetic demo bars the session falls back to, so bucket selection
    runs identically (just against synthetic data) with no network
    access at all.
    """
    if live_config.market_data_provider == "groww":
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
                "from_date": live_config.historical_from_date,
                "to_date": live_config.historical_to_date,
                "interval": live_config.historical_interval,
            },
        )

    if live_config.market_data_provider == "kite":
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
                "from_date": live_config.historical_from_date,
                "to_date": live_config.historical_to_date,
                "interval": live_config.historical_interval,
            },
        )

    return MarketDataConfig(
        provider="memory",
        mode="paper",
        metadata={"bars": build_session_bars(candidate_instruments)},
    )


def _ensure_live_transport_ready(broker: Broker) -> None:
    """
    V1 safety gate: fail fast, before streaming a single bar, if the
    selected broker cannot actually place live orders yet.

    This deliberately lives at the app/broker boundary rather than in
    ``ExecutionOrchestrator`` or the ``Broker`` interface: it inspects a
    concrete broker's capability flag, which is an app-level "should we
    even attempt this session" decision, not a generic execution concern.

    Duck-typed rather than an ``isinstance`` check against each concrete
    adapter (``GrowwBrokerAdapter``, ``KiteBrokerAdapter``, ...): every
    real broker adapter exposes ``mode`` and ``LIVE_TRANSPORT_IMPLEMENTED``
    (see either adapter's docstring), and ``PaperBroker`` has neither -
    ``getattr`` defaults make it transparently exempt without this
    function needing to import or know about every concrete adapter
    class that gets added over time.
    """
    if getattr(broker, "mode", None) == "live" and not getattr(broker, "LIVE_TRANSPORT_IMPLEMENTED", True):
        raise LiveTransportNotReadyError(
            f"Live trader wiring is complete and the broker factory correctly "
            f"built a {type(broker).__name__}(mode='live'), but its live "
            "transport is not implemented yet (_send_place_order/"
            "_send_cancel_order/_fetch_order/_fetch_positions are still "
            "stubs). Real live order placement is not available in this build."
        )


def _build_broker_config(
    live_config: LiveTraderConfig,
    groww_credentials: GrowwEnvCredentials | None,
    kite_credentials: KiteEnvCredentials | None,
) -> BrokerConfig:
    """
    Build the ``BrokerConfig`` for ``live_config.broker_provider``.

    ``"paper"`` needs no credentials at all - it's the safe default.
    ``"groww"``/``"kite"`` (real live brokers) reuse the credentials
    already loaded by ``build_live_session`` (only ever loaded when
    actually required - see ``LiveTraderConfig.requires_groww_credentials``/
    ``requires_kite_credentials``).
    """
    if live_config.broker_provider == "paper":
        return BrokerConfig(provider="paper", mode="paper")

    if live_config.broker_provider == "kite":
        return BrokerConfig(
            provider="kite",
            mode=live_config.broker_mode,
            metadata={
                "api_key": kite_credentials.api_key,
                "access_token": kite_credentials.access_token,
            },
        )

    return BrokerConfig(
        provider="groww",
        mode=live_config.broker_mode,
        metadata={
            "api_key": groww_credentials.api_key,
            "secret": groww_credentials.secret,
        },
    )


def _build_market_data_config(
    live_config: LiveTraderConfig,
    session_instruments: list[Instrument],
    groww_credentials: GrowwEnvCredentials | None,
    kite_credentials: KiteEnvCredentials | None,
) -> MarketDataConfig:
    """
    Build the ``MarketDataConfig`` for ``live_config.market_data_provider``.

    ``"groww"``/``"kite"`` are the real live paths: reuse the same
    credentials already loaded for the broker check (one account, one
    set of credentials, no separate market-data-only env vars to
    manage), and honor ``live_config.max_bars`` as a poll-count cap if
    set. ``"memory"`` is the safe local/dev path: deterministic demo
    bars, no network, no credentials required. This is never a silent
    fallback - which path runs is decided entirely by ``LiveTraderConfig``
    before this function is called, and the chosen provider is always
    printed in the startup banner.
    """
    if live_config.market_data_provider == "groww":
        metadata: dict[str, object] = {
            "api_key": groww_credentials.api_key,
            "secret": groww_credentials.secret,
        }
        if live_config.max_bars is not None:
            metadata["max_polls"] = live_config.max_bars

        return MarketDataConfig(provider="groww", mode="live", metadata=metadata)

    if live_config.market_data_provider == "kite":
        kite_metadata: dict[str, object] = {
            "api_key": kite_credentials.api_key,
            "access_token": kite_credentials.access_token,
        }
        if live_config.max_bars is not None:
            kite_metadata["max_polls"] = live_config.max_bars

        return MarketDataConfig(provider="kite", mode="live", metadata=kite_metadata)

    return MarketDataConfig(
        provider="memory",
        mode="paper",
        metadata={"bars": build_session_bars(session_instruments)},
    )


def build_live_session(
    mandate_path: str = MANDATE_PATH,
    universe_path: str = UNIVERSE_PATH,
    config: LiveTraderConfig | None = None,
) -> LiveSessionContext:
    """
    Build everything needed for a live trading session, without running
    it.

    ``config`` defaults to ``load_live_trader_config_from_env()`` - tests
    can inject a specific ``LiveTraderConfig`` directly to exercise a
    provider combination without patching every relevant environment
    variable. Loading/validating the config (including the live-broker
    confirmation gate) is always the first thing that happens, before
    the mandate, universe, or any credential is touched.

    Kept separate from ``run_live_session()`` so tests can construct and
    inspect a session - confirming credentials are loaded only when
    needed, the broker factory is invoked with the right config, and the
    same core pipeline the paper trader uses (allocation, universe
    selection, orchestrator) is wired - without streaming any bars or
    requiring a live broker.
    """
    live_config = config if config is not None else load_live_trader_config_from_env()

    # 0) refuse to start if the previous session never reached a terminal
    #    status - a pure local-filesystem check (no credential/broker call
    #    needed), so it runs first. See UncleanPriorSessionError.
    unclean_session_id = find_unclean_prior_session()
    if unclean_session_id is not None and not live_config.allow_unclean_prior_session:
        raise UncleanPriorSessionError(
            f"Refusing to start: the previous session '{unclean_session_id}' never "
            "reached a terminal status, meaning the process likely crashed hard "
            "(a handled exception is already recorded as 'failed' - this is the "
            "other case). Check data/live_sessions/"
            f"{unclean_session_id}/ and the broker's real position/order state "
            "before starting a new session (see apps/live_trader/kill_switch_main.py). "
            "If you've verified it's safe to proceed, set "
            "LIVE_ALLOW_UNCLEAN_PRIOR_SESSION=true."
        )

    # 1) load today's mandate and derive the day's allocation plan
    mandate = load_daily_mandate(mandate_path)
    allocation_plan = build_portfolio_allocation_plan(mandate)

    # 2) load the configured instrument universe and select this
    #    session's CANDIDATE instruments - every enabled instrument on the
    #    configured exchange (default "NSE"), not yet the tradable bucket.
    #    Bucket selection (step 4.5) narrows this down by capital/risk
    #    appetite, mirroring apps/paper_trader/main.py. The market data
    #    feed keys bars by symbol alone, not exchange+symbol, so a single
    #    exchange is still assumed throughout.
    universe = load_instrument_universe(universe_path)
    candidate_instruments = TradingUniverseSelector().select(
        universe, UniverseSelectionCriteria(exchange=live_config.universe_exchange)
    )

    # 3) load Groww/Kite credentials ONLY if the selected broker or
    #    market-data provider actually needs them - paper+memory needs
    #    none at all, and the two provider's credentials are never both
    #    needed at once (see _ALLOWED_PROVIDER_COMBINATIONS - "groww" and
    #    "kite" never mix within one session).
    groww_credentials: GrowwEnvCredentials | None = None
    if live_config.requires_groww_credentials:
        groww_credentials = load_groww_credentials_from_env()

    kite_credentials: KiteEnvCredentials | None = None
    if live_config.requires_kite_credentials:
        kite_credentials = load_kite_credentials_from_env()

    # 4) build the broker through the broker factory - never by
    #    instantiating a concrete broker class directly.
    broker_config = _build_broker_config(live_config, groww_credentials, kite_credentials)
    broker = build_broker(broker_config)
    _ensure_live_transport_ready(broker)

    # 4.5) reconcile against the broker's real state before this session
    #    assumes it's starting flat - see UnreconciledPositionsError and
    #    core/execution/reconciliation.py. No audit_sink is threaded
    #    through here: start_session() hasn't been called yet at this
    #    point in run_live_session() (see that function), so a dedicated
    #    RECONCILIATION_* event isn't recorded for this specific call
    #    site - the reconciliation result is still surfaced via the
    #    startup banner, and a raised UnreconciledPositionsError is still
    #    captured by run_live_session()'s existing failure handling.
    reconciliation = check_broker_reconciliation(broker)
    if not reconciliation.is_clean and not live_config.allow_unreconciled_positions:
        positions_desc = ", ".join(
            f"{p.exchange}:{p.symbol} qty={p.quantity}" for p in reconciliation.unexpected_positions
        )
        raise UnreconciledPositionsError(
            f"Refusing to start: broker already reports open positions this session "
            f"never placed: {positions_desc}. Either flatten them first (see "
            "apps/live_trader/kill_switch_main.py) or, if this is expected (e.g. an "
            "intentional overnight DELIVERY holding), set "
            "LIVE_ALLOW_UNRECONCILED_POSITIONS=true to proceed anyway."
        )

    # 4.7) select the day's tradable bucket from the candidates: the same
    #    capital-affordability + risk-appetite volatility screen the paper
    #    trader already applies (see core/market_data/bucket_selector.py) -
    #    previously skipped here entirely, meaning a live session could
    #    trade a symbol paper trading would have excluded as unaffordable
    #    or too volatile for the day's risk appetite. Uses the credentials
    #    already loaded in step 3 - no separate credential path.
    candidate_market_data_config = _build_candidate_market_data_config(
        live_config, candidate_instruments, groww_credentials, kite_credentials
    )
    candidate_feed = build_market_data_feed(candidate_market_data_config)
    price_history = {
        instrument.symbol: candidate_feed.get_history(instrument.symbol)
        for instrument in candidate_instruments
    }
    bucket_criteria = BucketSelectionCriteria(
        risk_appetite=mandate.risk_appetite,
        max_capital_per_trade=allocation_plan.max_capital_per_trade,
        max_bucket_size=allocation_plan.max_open_positions,
    )
    bucket_result = CapitalRiskBucketSelector().select(
        candidate_instruments, price_history, bucket_criteria
    )
    session_instruments = bucket_result.selected
    session_symbols = [instrument.symbol for instrument in session_instruments]

    # 5) build the market data feed through the market-data factory -
    #    never by instantiating a concrete feed directly. This is a
    #    *fresh* feed scoped to the bucket, distinct from the candidate
    #    feed above - for "groww" this is the real live streaming feed
    #    (the candidate feed above was historical, only for bucket
    #    selection); for "memory" this regenerates demo bars for just the
    #    bucket's instruments, matching apps/paper_trader/main.py's same
    #    "empty bucket subscribes to nothing" reasoning.
    market_data_config = _build_market_data_config(
        live_config, session_instruments, groww_credentials, kite_credentials
    )
    market_data_feed = build_market_data_feed(market_data_config)

    market_data_service = MarketDataService()
    market_data_service.register_feed("live_session", market_data_feed)

    # 6) initialize the remaining dependencies
    risk_manager = SimpleIntradayRiskManager()

    orchestrator = ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=risk_manager,
        broker=broker,
    )

    # 7) describe the session. execution_mode is set from broker_config,
    #    not allocation_plan.execution_mode - the mandate describes
    #    capital/risk for the day, not which broker/mode executes it.
    #    square_off_time comes from the mandate's force_square_off_at -
    #    previously parsed but never actually enforced anywhere, a real
    #    gap for an intraday-only platform (see
    #    core/portfolio/exit_models.py's parse_time_of_day).
    square_off_time = (
        parse_time_of_day(mandate.risk_limits.force_square_off_at)
        if mandate.risk_limits.force_square_off_at
        else None
    )
    exit_policy = ExitPolicyConfig(stop_loss_pct=1.5, target_pct=2.0, square_off_time=square_off_time)

    session_config = TradingSessionConfig(
        trading_date=allocation_plan.trading_date,
        strategy_name="simple_breakout_momentum",
        strategy_params={},
        execution_mode=broker_config.mode,
        symbols=session_symbols,
        portfolio_allocation_plan=allocation_plan,
        exit_policy=exit_policy,
    )

    return LiveSessionContext(
        live_config=live_config,
        allocation_plan=allocation_plan,
        universe=universe,
        candidate_instruments=candidate_instruments,
        bucket_result=bucket_result,
        session_instruments=session_instruments,
        session_symbols=session_symbols,
        broker_config=broker_config,
        broker=broker,
        reconciliation=reconciliation,
        market_data_config=market_data_config,
        orchestrator=orchestrator,
        session_config=session_config,
    )


def print_startup_banner(ctx: LiveSessionContext) -> None:
    """
    Print the pre-flight session summary: everything an operator needs
    to confirm this is the session they intend to run, before any bar
    is streamed or order is placed.
    """
    plan = ctx.allocation_plan
    print("=== LIVE TRADER START ===")
    print(f"Trading date: {plan.trading_date}")
    print(f"Broker: {ctx.broker_config.provider}/{ctx.broker_config.mode}")
    print(f"Market data: {ctx.market_data_config.provider}/{ctx.market_data_config.mode}")
    print(f"Dry run: {ctx.live_config.dry_run}")
    print(f"Universe: {ctx.universe.universe_name}")
    print(f"Intraday capital: {plan.intraday_capital}")
    print(f"Max daily loss: {plan.max_daily_loss}")
    print(f"Max trades/day: {plan.max_trades_per_day}")
    print(f"Max open positions: {plan.max_open_positions}")
    print(f"Execution mode: {ctx.session_config.execution_mode}")
    if ctx.reconciliation.is_clean:
        print("Reconciliation: clean (broker reports no open positions)")
    else:
        positions_desc = ", ".join(
            f"{p.exchange}:{p.symbol} qty={p.quantity}" for p in ctx.reconciliation.unexpected_positions
        )
        print(f"Reconciliation: UNRECONCILED, proceeding anyway (allowed) - {positions_desc}")
    print()

    print("=== BUCKET SELECTION ===")
    print(f"Candidates considered ({len(ctx.candidate_instruments)}): "
          f"{', '.join(i.symbol for i in ctx.candidate_instruments)}")
    print(f"Subscribed symbols ({len(ctx.session_symbols)}): {', '.join(ctx.session_symbols) or 'none'}")
    if ctx.bucket_result.excluded:
        print(f"Excluded ({len(ctx.bucket_result.excluded)}):")
        for exclusion in ctx.bucket_result.excluded:
            print(f"  -> {exclusion.exchange}:{exclusion.symbol} reason={exclusion.reason}")
    print()


def print_session_summary(result: ExecutionSessionResult) -> None:
    """Print the multi-symbol session summary, mirroring the paper trader's report."""
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
    print("=== LIVE TRADER END ===")


def _generate_session_id() -> str:
    """Human-sortable, unique session id: <UTC timestamp>_<short random suffix>."""
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{uuid4().hex[:8]}"


def _build_session_metadata(session_id: str, started_at: datetime, ctx: LiveSessionContext) -> SessionMetadata:
    """
    Build the session identity/config snapshot to persist once a full
    ``LiveSessionContext`` is available.

    Deliberately hand-picks safe fields from ``ctx.broker_config``/
    ``ctx.market_data_config`` rather than persisting those objects'
    ``metadata`` dicts wholesale - those carry API keys/access tokens
    that must never reach disk. ``allocation_plan``/``exit_policy``
    contain no secrets and are safe to persist directly.
    """
    return SessionMetadata(
        session_id=session_id,
        app_name="live_trader",
        started_at=started_at,
        trading_date=ctx.allocation_plan.trading_date,
        strategy_name=ctx.session_config.strategy_name,
        broker_provider=ctx.broker_config.provider,
        broker_mode=ctx.broker_config.mode,
        market_data_provider=ctx.market_data_config.provider,
        market_data_mode=ctx.market_data_config.mode,
        universe_name=ctx.universe.universe_name,
        subscribed_symbols=list(ctx.session_symbols),
        allocation_plan=ctx.allocation_plan,
        exit_policy=ctx.session_config.exit_policy,
    )


def _build_session_summary(result: ExecutionSessionResult, succeeded: bool) -> SessionSummary:
    """Compose the persisted final summary from the orchestrator's ExecutionSessionResult."""
    return SessionSummary(
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
        succeeded=succeeded,
    )


def _build_dry_run_summary(ctx: LiveSessionContext) -> SessionSummary:
    """
    A zeroed ``SessionSummary`` for a dry run: the session was fully
    built and pre-flight-validated (mandate, universe, credentials,
    broker, market data feed, orchestrator all constructed successfully)
    but ``orchestrator.run_session()`` was never called, so there is no
    real activity to report.
    """
    return SessionSummary(
        strategy_name=ctx.session_config.strategy_name,
        trading_date=ctx.session_config.trading_date,
        processed_bars=0,
        generated_signals=0,
        symbols_with_signals=[],
        approved_signals=0,
        rejected_signals=0,
        skipped_due_to_session_guardrails=0,
        skipped_due_to_position_limits=0,
        placed_orders=[],
        exit_decisions=[],
        exit_orders=[],
        final_positions=[],
        final_session_risk_state=None,
        succeeded=True,
    )


def run_live_session(
    audit_sink: AuditSink | None = None, alert_sink: AlertSink | None = None
) -> ExecutionSessionResult | None:
    """
    Build a live session, print the pre-flight banner, run it (unless
    ``dry_run``), and print the summary - persisting a durable session
    record throughout.

    Returns ``None`` for a dry run (no session was actually executed);
    otherwise the real ``ExecutionSessionResult``.

    ``audit_sink`` defaults to a real ``FileAuditSink`` (tests inject an
    in-memory double to avoid touching the filesystem). The session
    record is started before ``build_live_session()`` is even called, so
    a failure there (e.g. missing credentials, an unconfirmed live
    broker, an invalid provider combination) is still captured, just
    with a leaner metadata snapshot than a failure once the full session
    context is known - see the two ``except`` blocks below. ``alert_sink``
    defaults to a real ``ConsoleAlertSink`` and fires on every path that
    ends this session without a real result - "session died, ping me."
    """
    sink = audit_sink if audit_sink is not None else FileAuditSink()
    alerts = alert_sink if alert_sink is not None else ConsoleAlertSink()
    session_id = _generate_session_id()
    started_at = datetime.now(UTC)

    try:
        ctx = build_live_session()
    except Exception as exc:
        sink.start_session(
            SessionMetadata(session_id=session_id, app_name="live_trader", started_at=started_at)
        )
        sink.record_failure(exc)
        alerts.send(
            f"live_trader session '{session_id}' failed before it could start: "
            f"{type(exc).__name__}: {exc}",
            severity="critical",
        )
        raise

    sink.start_session(_build_session_metadata(session_id, started_at, ctx))
    ctx.orchestrator.audit_sink = sink

    try:
        print_startup_banner(ctx)

        if ctx.live_config.dry_run:
            print("=== DRY RUN: session validated successfully, stopping before execution ===")
            print()
            sink.finalize_session(_build_dry_run_summary(ctx))
            return None

        result = ctx.orchestrator.run_session(ctx.session_config)
        print_session_summary(result)

        sink.finalize_session(_build_session_summary(result, succeeded=True))
        if result.errors_encountered:
            alerts.send(
                f"live_trader session '{session_id}' completed but skipped "
                f"{result.errors_encountered} bar(s) due to errors - check "
                "ORCHESTRATOR_ERROR events in the session's event log.",
                severity="warning",
            )
        return result
    except Exception as exc:
        sink.record_failure(exc)
        alerts.send(
            f"live_trader session '{session_id}' died: {type(exc).__name__}: {exc}",
            severity="critical",
        )
        raise


def main() -> None:
    load_secrets_into_env()
    run_live_session()


if __name__ == "__main__":
    main()

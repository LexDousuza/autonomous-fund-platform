from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from apps.live_trader.config import (
    GrowwEnvCredentials,
    LiveTraderConfig,
    load_groww_credentials_from_env,
    load_live_trader_config_from_env,
)
from core.allocations.daily_mandate import load_daily_mandate
from core.audit.file_sink import FileAuditSink
from core.audit.models import SessionMetadata, SessionSummary
from core.audit.sink import AuditSink
from core.broker.base import Broker
from core.broker.factory import build_broker
from core.broker.factory_models import BrokerConfig
from core.broker.groww_adapter import GrowwBrokerAdapter
from core.execution.models import ExecutionSessionResult, TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.factory import build_market_data_feed
from core.market_data.factory_models import MarketDataConfig
from core.market_data.service import MarketDataService
from core.market_data.universe_loader import load_instrument_universe
from core.market_data.universe_models import Instrument, InstrumentUniverse
from core.market_data.universe_selector import TradingUniverseSelector, UniverseSelectionCriteria
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.portfolio.exit_models import ExitPolicyConfig
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


@dataclass(slots=True)
class LiveSessionContext:
    """Everything needed to run (or refuse to run) a live trading session."""

    live_config: LiveTraderConfig
    allocation_plan: PortfolioAllocationPlan
    universe: InstrumentUniverse
    session_instruments: list[Instrument]
    session_symbols: list[str]
    broker_config: BrokerConfig
    broker: Broker
    market_data_config: MarketDataConfig
    orchestrator: ExecutionOrchestrator
    session_config: TradingSessionConfig


def build_session_bars(instruments: list[Instrument]) -> list[MarketBar]:
    """
    Build a small deterministic, interleaved multi-symbol intraday bar
    stream, standing in for a live feed.

    This is the same demo shape used by the paper trader (a base bar, a
    breakout bar, then a bar that rallies past the exit engine's profit
    target, per instrument at a distinct price level). Used only for the
    safe local/dev market-data path (``LIVE_MARKET_DATA_PROVIDER=memory``,
    the default) - see ``_build_market_data_config``. The real live path
    (``LIVE_MARKET_DATA_PROVIDER=groww``) uses ``GrowwLiveMarketDataFeed``
    instead and never touches these bars.
    """
    base_time = datetime.now().replace(second=0, microsecond=0)
    bars: list[MarketBar] = []

    for offset in range(3):
        timestamp = base_time + timedelta(minutes=offset)
        for index, instrument in enumerate(instruments):
            base_price = 500.0 + index * 300.0

            if offset == 0:
                open_, high, low, close, volume = (
                    base_price - 10.0,
                    base_price + 5.0,
                    base_price - 15.0,
                    base_price - 2.0,
                    100000,
                )
            elif offset == 1:
                open_, high, low, close, volume = (
                    base_price,
                    base_price + 20.0,
                    base_price - 5.0,
                    base_price + 11.0,  # breakout above bar-0 high, on higher volume
                    150000,
                )
            else:
                open_, high, low, close, volume = (
                    base_price + 15.0,
                    base_price + 205.0,
                    base_price + 10.0,
                    base_price + 200.0,  # rallies well past the profit target
                    120000,
                )

            bars.append(
                MarketBar(
                    symbol=instrument.symbol,
                    exchange=instrument.exchange,
                    timestamp=timestamp,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                )
            )

    return bars


def _ensure_live_transport_ready(broker: Broker) -> None:
    """
    V1 safety gate: fail fast, before streaming a single bar, if the
    selected broker cannot actually place live orders yet.

    This deliberately lives at the app/broker boundary rather than in
    ``ExecutionOrchestrator`` or the ``Broker`` interface: it inspects a
    concrete broker's capability flag, which is an app-level "should we
    even attempt this session" decision, not a generic execution concern.
    """
    if isinstance(broker, GrowwBrokerAdapter) and broker.mode == "live":
        if not GrowwBrokerAdapter.LIVE_TRANSPORT_IMPLEMENTED:
            raise LiveTransportNotReadyError(
                "Live trader wiring is complete and the broker factory "
                "correctly built a GrowwBrokerAdapter(mode='live'), but "
                "Groww's live transport is not implemented yet "
                "(_send_place_order/_send_cancel_order/_fetch_order/"
                "_fetch_positions are still stubs). Real live order "
                "placement is not available in this build."
            )


def _build_broker_config(
    live_config: LiveTraderConfig,
    credentials: GrowwEnvCredentials | None,
) -> BrokerConfig:
    """
    Build the ``BrokerConfig`` for ``live_config.broker_provider``.

    ``"paper"`` needs no credentials at all - it's the safe default.
    ``"groww"`` (real live broker) reuses the credentials already loaded
    by ``build_live_session`` (only ever loaded when actually required -
    see ``LiveTraderConfig.requires_groww_credentials``).
    """
    if live_config.broker_provider == "paper":
        return BrokerConfig(provider="paper", mode="paper")

    return BrokerConfig(
        provider="groww",
        mode=live_config.broker_mode,
        metadata={
            "api_key": credentials.api_key,
            "access_token": credentials.access_token,
            "base_url": credentials.base_url,
        },
    )


def _build_market_data_config(
    live_config: LiveTraderConfig,
    session_instruments: list[Instrument],
    credentials: GrowwEnvCredentials | None,
) -> MarketDataConfig:
    """
    Build the ``MarketDataConfig`` for ``live_config.market_data_provider``.

    ``"groww"`` is the real live path: reuses the same Groww credentials
    already loaded for the broker check (one account, one set of
    credentials, no separate market-data-only env vars to manage), and
    honors ``live_config.max_bars`` as a poll-count cap if set. ``"memory"``
    is the safe local/dev path: deterministic demo bars, no network, no
    credentials required. This is never a silent fallback - which path
    runs is decided entirely by ``LiveTraderConfig`` before this function
    is called, and the chosen provider is always printed in the startup
    banner.
    """
    if live_config.market_data_provider == "groww":
        metadata: dict[str, object] = {
            "api_key": credentials.api_key,
            "access_token": credentials.access_token,
            "base_url": credentials.base_url,
        }
        if live_config.max_bars is not None:
            metadata["max_polls"] = live_config.max_bars

        return MarketDataConfig(provider="groww", mode="live", metadata=metadata)

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

    # 1) load today's mandate and derive the day's allocation plan
    mandate = load_daily_mandate(mandate_path)
    allocation_plan = build_portfolio_allocation_plan(mandate)

    # 2) load the configured instrument universe and select this
    #    session's tradable symbols. Exchange is configurable (default
    #    "NSE") but still a single exchange: the market data feed keys
    #    bars by symbol alone, not exchange+symbol.
    universe = load_instrument_universe(universe_path)
    session_instruments = TradingUniverseSelector().select(
        universe, UniverseSelectionCriteria(exchange=live_config.universe_exchange)
    )
    session_symbols = [instrument.symbol for instrument in session_instruments]

    # 3) load Groww credentials ONLY if the selected broker or market-data
    #    provider actually needs them - paper+memory needs none at all.
    credentials: GrowwEnvCredentials | None = None
    if live_config.requires_groww_credentials:
        credentials = load_groww_credentials_from_env()

    # 4) build the broker through the broker factory - never by
    #    instantiating a concrete broker class directly.
    broker_config = _build_broker_config(live_config, credentials)
    broker = build_broker(broker_config)
    _ensure_live_transport_ready(broker)

    # 5) build the market data feed through the market-data factory -
    #    never by instantiating a concrete feed directly.
    market_data_config = _build_market_data_config(live_config, session_instruments, credentials)
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
    exit_policy = ExitPolicyConfig(stop_loss_pct=1.5, target_pct=2.0)

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
        session_instruments=session_instruments,
        session_symbols=session_symbols,
        broker_config=broker_config,
        broker=broker,
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
    print(f"Subscribed symbols ({len(ctx.session_symbols)}): {', '.join(ctx.session_symbols)}")
    print(f"Intraday capital: {plan.intraday_capital}")
    print(f"Max daily loss: {plan.max_daily_loss}")
    print(f"Max trades/day: {plan.max_trades_per_day}")
    print(f"Max open positions: {plan.max_open_positions}")
    print(f"Execution mode: {ctx.session_config.execution_mode}")
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


def run_live_session(audit_sink: AuditSink | None = None) -> ExecutionSessionResult | None:
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
    context is known - see the two ``except`` blocks below.
    """
    sink = audit_sink if audit_sink is not None else FileAuditSink()
    session_id = _generate_session_id()
    started_at = datetime.now(UTC)

    try:
        ctx = build_live_session()
    except Exception as exc:
        sink.start_session(
            SessionMetadata(session_id=session_id, app_name="live_trader", started_at=started_at)
        )
        sink.record_failure(exc)
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
        return result
    except Exception as exc:
        sink.record_failure(exc)
        raise


def main() -> None:
    run_live_session()


if __name__ == "__main__":
    main()

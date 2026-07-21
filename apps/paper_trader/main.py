from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from apps.live_trader.config import GrowwEnvCredentials, load_groww_credentials_from_env
from apps.paper_trader.config import PaperTraderConfig, load_paper_trader_config_from_env
from core.allocations.daily_mandate import DailyMandate, load_daily_mandate
from core.audit.file_sink import FileAuditSink
from core.audit.models import SessionMetadata, SessionSummary
from core.audit.sink import AuditSink
from core.broker.base import Broker
from core.broker.factory import build_broker
from core.broker.factory_models import BrokerConfig
from core.execution.models import ExecutionSessionResult, TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.bucket_selector import (
    BucketSelectionCriteria,
    BucketSelectionResult,
    CapitalRiskBucketSelector,
)
from core.market_data.factory import build_market_data_feed
from core.market_data.factory_models import MarketDataConfig
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.market_data.universe_loader import load_instrument_universe
from core.market_data.universe_models import Instrument
from core.market_data.universe_selector import TradingUniverseSelector, UniverseSelectionCriteria
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.portfolio.exit_models import ExitPolicyConfig, parse_time_of_day
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.simple_intraday import SimpleIntradayRiskManager
from core.strategy.base import MarketBar
import core.strategy.simple_breakout_momentum  # noqa: F401  (registers the strategy)

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
UNIVERSE_PATH = "config/instruments/india_cash_universe.yaml"


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
    credentials: GrowwEnvCredentials | None,
) -> MarketDataConfig:
    """
    Build the ``MarketDataConfig`` covering every *candidate* instrument
    (not yet the bucket) - bucket selection needs each candidate's price
    history before it can decide which ones make the cut.

    ``"groww"`` fetches real historical candles for the configured date
    range/interval. ``"memory"`` (the default) generates the same
    synthetic demo bars the paper trader has always used, just for the
    full candidate set instead of a pre-filtered one - bucket selection
    runs identically either way, since it only cares about price history,
    not where it came from.
    """
    if config.market_data_provider == "groww":
        return MarketDataConfig(
            provider="groww",
            mode="historical",
            metadata={
                "api_key": credentials.api_key,
                "secret": credentials.secret,
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
    #    overrides (if any), and derive the day's allocation plan.
    mandate = load_daily_mandate(mandate_path)
    _apply_config_overrides(mandate, paper_config)
    allocation_plan = build_portfolio_allocation_plan(mandate)

    # 2) load the configured instrument universe and select this
    #    session's CANDIDATE instruments - every enabled NSE instrument,
    #    not yet the tradable bucket. Bucket selection (step 4) narrows
    #    this down by capital/risk appetite.
    universe = load_instrument_universe(universe_path)
    candidate_instruments = TradingUniverseSelector().select(
        universe, UniverseSelectionCriteria(exchange="NSE")
    )

    # 3) load Groww credentials only if real historical data was
    #    requested, and build+fetch price history for every candidate -
    #    bucket selection needs prices before it can judge affordability/
    #    volatility for any of them.
    credentials: GrowwEnvCredentials | None = None
    if paper_config.requires_groww_credentials:
        credentials = load_groww_credentials_from_env()

    market_data_config = _build_candidate_market_data_config(
        paper_config, candidate_instruments, credentials
    )
    market_data_feed = build_market_data_feed(market_data_config)
    price_history = {
        instrument.symbol: market_data_feed.get_history(instrument.symbol)
        for instrument in candidate_instruments
    }

    # 4) select the day's tradable bucket from the candidates: hard
    #    affordability filter plus a volatility ceiling driven by
    #    mandate.risk_appetite, capped at the allocation plan's max open
    #    positions (no point selecting more names than positions the
    #    session could ever hold at once).
    bucket_criteria = BucketSelectionCriteria(
        risk_appetite=mandate.risk_appetite,
        max_capital_per_trade=allocation_plan.max_capital_per_trade,
        max_bucket_size=allocation_plan.max_open_positions,
    )
    bucket_result = CapitalRiskBucketSelector().select(
        candidate_instruments, price_history, bucket_criteria
    )
    session_symbols = [instrument.symbol for instrument in bucket_result.selected]

    # 5) build the actual SESSION feed from only the bucket's already-
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

    # 6) initialize dependencies. The broker is built through the broker
    #    factory rather than instantiated directly.
    broker_config = BrokerConfig(provider="paper", mode="paper")
    broker = build_broker(broker_config)
    risk_manager = SimpleIntradayRiskManager()

    market_data_service = MarketDataService()
    market_data_service.register_feed("paper_session", MemoryMarketDataFeed(bars=session_bars))

    orchestrator = ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=risk_manager,
        broker=broker,
    )

    # 7) describe the session (including V1 intraday exit rules) and hand
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
    exit_policy = ExitPolicyConfig(stop_loss_pct=1.5, target_pct=2.0, square_off_time=square_off_time)

    session_config = TradingSessionConfig(
        trading_date=allocation_plan.trading_date,
        strategy_name="simple_breakout_momentum",
        strategy_params={},
        execution_mode=allocation_plan.execution_mode,
        symbols=session_symbols,
        portfolio_allocation_plan=allocation_plan,
        exit_policy=exit_policy,
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


def main() -> None:
    run_paper_session()


if __name__ == "__main__":
    main()

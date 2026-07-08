from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from apps.live_trader.config import (
    GrowwEnvCredentials,
    load_groww_credentials_from_env,
    load_market_data_provider_from_env,
)
from core.allocations.daily_mandate import load_daily_mandate
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


def _build_market_data_config(
    provider: str,
    session_instruments: list[Instrument],
    credentials: GrowwEnvCredentials,
) -> MarketDataConfig:
    """
    Build the ``MarketDataConfig`` for ``provider``.

    ``provider="groww"`` is the real live path: reuses the same Groww
    credentials already loaded for the broker (one account, one set of
    credentials, no separate market-data-only env vars to manage).
    Anything else - including the default, ``"memory"`` - is the safe
    local/dev path: deterministic demo bars, no network, no credentials
    required. This is never a silent fallback: which path runs is decided
    entirely by ``LIVE_MARKET_DATA_PROVIDER`` before this function is
    called, and the chosen provider is always printed in the startup
    banner.
    """
    if provider == "groww":
        return MarketDataConfig(
            provider="groww",
            mode="live",
            metadata={
                "api_key": credentials.api_key,
                "access_token": credentials.access_token,
                "base_url": credentials.base_url,
            },
        )

    return MarketDataConfig(
        provider="memory",
        mode="paper",
        metadata={"bars": build_session_bars(session_instruments)},
    )


def build_live_session(
    mandate_path: str = MANDATE_PATH,
    universe_path: str = UNIVERSE_PATH,
) -> LiveSessionContext:
    """
    Build everything needed for a live trading session, without running
    it.

    Kept separate from ``run_live_session()`` so tests can construct and
    inspect a session - confirming credentials are loaded, the broker
    factory is invoked with the right config, and the same core pipeline
    the paper trader uses (allocation, universe selection, orchestrator)
    is wired - without streaming any bars or requiring a live broker.
    """
    # 1) load today's mandate and derive the day's allocation plan
    mandate = load_daily_mandate(mandate_path)
    allocation_plan = build_portfolio_allocation_plan(mandate)

    # 2) load the configured instrument universe and select this
    #    session's tradable symbols (same NSE-only rationale as the
    #    paper trader: the market data feed keys bars by symbol alone).
    universe = load_instrument_universe(universe_path)
    session_instruments = TradingUniverseSelector().select(
        universe, UniverseSelectionCriteria(exchange="NSE")
    )
    session_symbols = [instrument.symbol for instrument in session_instruments]

    # 3) load credentials from the environment (app-level concern, not
    #    core), and build the broker through the broker factory - never
    #    by instantiating GrowwBrokerAdapter directly.
    credentials = load_groww_credentials_from_env()
    broker_config = BrokerConfig(
        provider="groww",
        mode="live",
        metadata={
            "api_key": credentials.api_key,
            "access_token": credentials.access_token,
            "base_url": credentials.base_url,
        },
    )
    broker = build_broker(broker_config)
    _ensure_live_transport_ready(broker)

    # 4) build the market data feed through the market-data factory -
    #    never by instantiating a concrete feed directly. Provider is an
    #    explicit environment choice (default: "memory", the safe local/
    #    dev path); only LIVE_MARKET_DATA_PROVIDER=groww attempts real
    #    live market data, reusing the same Groww credentials as the
    #    broker.
    market_data_provider = load_market_data_provider_from_env()
    market_data_config = _build_market_data_config(
        market_data_provider, session_instruments, credentials
    )
    market_data_feed = build_market_data_feed(market_data_config)

    market_data_service = MarketDataService()
    market_data_service.register_feed("live_session", market_data_feed)

    # 5) initialize the remaining dependencies
    risk_manager = SimpleIntradayRiskManager()

    orchestrator = ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=risk_manager,
        broker=broker,
    )

    # 6) describe the session. execution_mode is set from broker_config,
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
    """Print the live-session startup banner (trading date, capital, universe, broker, market data)."""
    print("=== LIVE TRADER START ===")
    print(f"Trading date: {ctx.allocation_plan.trading_date}")
    print(f"Intraday capital: {ctx.allocation_plan.intraday_capital}")
    print(f"Execution mode: {ctx.session_config.execution_mode}")
    print(f"Universe: {ctx.universe.universe_name}")
    print(f"Subscribed symbols ({len(ctx.session_symbols)}): {', '.join(ctx.session_symbols)}")
    print(f"Broker: {ctx.broker_config.provider}/{ctx.broker_config.mode}")
    print(f"Market data: {ctx.market_data_config.provider}/{ctx.market_data_config.mode}")
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


def run_live_session() -> ExecutionSessionResult:
    """Build a live session, print the startup banner, run it, and print the summary."""
    ctx = build_live_session()
    print_startup_banner(ctx)

    result = ctx.orchestrator.run_session(ctx.session_config)
    print_session_summary(result)

    return result


def main() -> None:
    run_live_session()


if __name__ == "__main__":
    main()

from __future__ import annotations

from datetime import datetime, timedelta

from core.allocations.daily_mandate import load_daily_mandate
from core.broker.paper import PaperBroker
from core.execution.models import TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.market_data.universe_loader import load_instrument_universe
from core.market_data.universe_models import Instrument
from core.market_data.universe_selector import TradingUniverseSelector, UniverseSelectionCriteria
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.portfolio.exit_models import ExitPolicyConfig
from core.risk.simple_intraday import SimpleIntradayRiskManager
from core.strategy.base import MarketBar
import core.strategy.simple_breakout_momentum  # noqa: F401  (registers the strategy)


def build_session_bars(instruments: list[Instrument]) -> list[MarketBar]:
    """
    Build a small deterministic, interleaved multi-symbol intraday bar
    stream, standing in for a live feed.

    For each instrument this replays the same three-bar shape as the
    original single-symbol demo (a base bar, a breakout bar, then a bar
    that rallies past the exit engine's profit target) at a distinct
    price level per symbol, so the scenario is clearly multi-symbol while
    staying deterministic. Bars are interleaved by time offset (all
    symbols' bar 0, then all symbols' bar 1, ...) to mimic a live feed
    streaming several symbols roughly simultaneously.
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


def main() -> None:
    # 1) load today's mandate and derive the day's allocation plan
    mandate = load_daily_mandate("config/mandates/sample_mandate.yaml")
    allocation_plan = build_portfolio_allocation_plan(mandate)

    # 2) load the configured instrument universe and select this
    #    session's tradable symbols. Restricted to NSE: the market data
    #    feed keys bars/subscriptions by symbol alone (not exchange +
    #    symbol), so a cross-exchange duplicate like BSE:RELIANCE isn't
    #    distinguishable from NSE:RELIANCE yet - a known V1 limitation.
    #    The configured universe has no duplicate symbols within NSE.
    universe = load_instrument_universe("config/instruments/india_cash_universe.yaml")
    session_instruments = TradingUniverseSelector().select(
        universe, UniverseSelectionCriteria(exchange="NSE")
    )
    session_symbols = [instrument.symbol for instrument in session_instruments]

    # 3) initialize dependencies
    broker = PaperBroker()
    risk_manager = SimpleIntradayRiskManager()

    market_data_service = MarketDataService()
    market_data_service.register_feed(
        "paper_session",
        MemoryMarketDataFeed(bars=build_session_bars(session_instruments)),
    )

    orchestrator = ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=risk_manager,
        broker=broker,
    )

    # 4) describe the session (including V1 intraday exit rules) and hand
    #    it to the orchestrator. Order quantity is not set here:
    #    ExecutionOrchestrator derives it per signal via
    #    PositionSizingEngine, from allocation_plan.max_capital_per_trade
    #    and the triggering bar's price. Open-position/exposure limits
    #    (allocation_plan.max_open_positions / max_symbol_allocation) are
    #    enforced by the orchestrator too.
    exit_policy = ExitPolicyConfig(stop_loss_pct=1.5, target_pct=2.0)

    session_config = TradingSessionConfig(
        trading_date=allocation_plan.trading_date,
        strategy_name="simple_breakout_momentum",
        strategy_params={},
        execution_mode=allocation_plan.execution_mode,
        symbols=session_symbols,
        portfolio_allocation_plan=allocation_plan,
        exit_policy=exit_policy,
    )

    result = orchestrator.run_session(session_config)

    # 5) print a multi-symbol session summary
    print("=== PAPER TRADER START ===")
    print(f"Trading date: {allocation_plan.trading_date}")
    print(f"Intraday capital: {allocation_plan.intraday_capital}")
    print(f"Execution mode: {allocation_plan.execution_mode}")
    print(f"Universe: {universe.universe_name}")
    print(f"Subscribed symbols ({len(session_symbols)}): {', '.join(session_symbols)}")
    print()

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
    print("=== PAPER TRADER END ===")


if __name__ == "__main__":
    main()

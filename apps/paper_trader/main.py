from __future__ import annotations

from datetime import datetime, timedelta

from core.allocations.daily_mandate import load_daily_mandate
from core.broker.paper import PaperBroker
from core.execution.models import TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.risk.simple_intraday import SimpleIntradayRiskManager
from core.strategy.base import MarketBar
import core.strategy.simple_breakout_momentum  # noqa: F401  (registers the strategy)


def build_session_bars() -> list[MarketBar]:
    """A tiny fake intraday stream of bars, standing in for a live feed."""
    base_time = datetime.now().replace(second=0, microsecond=0)

    return [
        MarketBar(
            symbol="RELIANCE",
            exchange="NSE",
            timestamp=base_time,
            open=2480.0,
            high=2495.0,
            low=2475.0,
            close=2490.0,
            volume=100000,
        ),
        MarketBar(
            symbol="RELIANCE",
            exchange="NSE",
            timestamp=base_time + timedelta(minutes=1),
            open=2492.0,
            high=2510.0,
            low=2490.0,
            close=2501.0,  # breakout above previous high
            volume=150000,  # stronger volume
        ),
    ]


def main() -> None:
    # 1) load today's mandate and derive the day's allocation plan
    mandate = load_daily_mandate("config/mandates/sample_mandate.yaml")
    allocation_plan = build_portfolio_allocation_plan(mandate)

    # 2) initialize dependencies
    broker = PaperBroker()
    risk_manager = SimpleIntradayRiskManager()

    market_data_service = MarketDataService()
    market_data_service.register_feed(
        "paper_session",
        MemoryMarketDataFeed(bars=build_session_bars()),
    )

    orchestrator = ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=risk_manager,
        broker=broker,
    )

    # 3) describe the session and hand it to the orchestrator
    session_config = TradingSessionConfig(
        trading_date=allocation_plan.trading_date,
        strategy_name="simple_breakout_momentum",
        strategy_params={},
        execution_mode=allocation_plan.execution_mode,
        symbols=["RELIANCE"],
        portfolio_allocation_plan=allocation_plan,
        quantity_per_trade=5,
    )

    result = orchestrator.run_session(session_config)

    # 4) print a summary
    print("=== PAPER TRADER START ===")
    print(f"Trading date: {allocation_plan.trading_date}")
    print(f"Intraday capital: {allocation_plan.intraday_capital}")
    print(f"Execution mode: {allocation_plan.execution_mode}")
    print()

    print("=== SESSION SUMMARY ===")
    print(f"Strategy: {result.strategy_name}")
    print(f"Bars processed: {result.processed_bars}")
    print(f"Signals generated: {result.generated_signals}")
    print(f"Signals approved: {result.approved_signals}")
    print(f"Signals rejected: {result.rejected_signals}")
    print(f"Orders placed: {len(result.placed_orders)}")
    for order in result.placed_orders:
        print(
            f"  -> id={order.order_id} status={order.status.value} "
            f"avg_price={order.average_price}"
        )
    print()

    print("=== FINAL POSITIONS ===")
    if not result.final_positions:
        print("No positions")
    else:
        for p in result.final_positions:
            print(
                f"{p.exchange}:{p.symbol} | qty={p.quantity} | "
                f"avg={p.average_price} | product={p.product_type.value}"
            )

    print()
    print("=== PAPER TRADER END ===")


if __name__ == "__main__":
    main()

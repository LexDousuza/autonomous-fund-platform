from __future__ import annotations

from datetime import datetime, timedelta

from core.allocations.daily_mandate import load_daily_mandate
from core.broker.base import OrderRequest, OrderSide, OrderType, ProductType
from core.broker.paper import PaperBroker
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.risk.base import RiskContext
from core.risk.simple_intraday import SimpleIntradayRiskManager
from core.strategy.base import MarketBar, SignalSide, StrategyContext
from core.strategy.simple_breakout_momentum import SimpleBreakoutMomentumStrategy


def build_order_from_signal(signal, quantity: int, price: float) -> OrderRequest:
    if signal.side == SignalSide.BUY:
        side = OrderSide.BUY
    elif signal.side == SignalSide.SELL:
        side = OrderSide.SELL
    else:
        raise ValueError("EXIT signals are not yet supported in V1 order conversion")

    return OrderRequest(
        symbol=signal.symbol,
        exchange=signal.exchange,
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY,
        price=price,
        strategy_name=signal.strategy_name,
        metadata={"signal_reason": signal.reason, **signal.metadata},
    )


def main() -> None:
    # 1) load today's mandate and derive the day's allocation plan
    mandate = load_daily_mandate("config/mandates/sample_mandate.yaml")
    allocation_plan = build_portfolio_allocation_plan(mandate)

    # 2) create components
    strategy = SimpleBreakoutMomentumStrategy(
        StrategyContext(strategy_name="simple_breakout_momentum")
    )
    risk_manager = SimpleIntradayRiskManager()
    broker = PaperBroker()

    # 3) create a tiny fake intraday stream of bars
    base_time = datetime.now().replace(second=0, microsecond=0)

    bars = [
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

    history: list[MarketBar] = []
    trades_taken_today = 0
    used_intraday_capital = 0.0
    daily_realized_pnl = 0.0

    print("=== PAPER TRADER START ===")
    print(f"Trading date: {allocation_plan.trading_date}")
    print(f"Intraday capital: {allocation_plan.intraday_capital}")
    print(f"Execution mode: {allocation_plan.execution_mode}")
    print()

    for bar in bars:
        print(
            f"[BAR] {bar.exchange}:{bar.symbol} "
            f"time={bar.timestamp.strftime('%H:%M')} "
            f"close={bar.close} volume={bar.volume}"
        )

        open_positions = {
            f"{p.exchange}:{p.symbol}": p for p in broker.list_positions() if p.quantity != 0
        }

        signals = strategy.on_bar(
            bar=bar,
            history=history,
            open_positions=open_positions,
        )

        if not signals:
            print("  -> No signal")
            history.append(bar)
            continue

        for signal in signals:
            print(f"  -> Signal generated: {signal.side.value} | reason={signal.reason}")

            # For V1, fixed quantity for demonstration
            order = build_order_from_signal(signal, quantity=5, price=bar.close)

            risk_context = RiskContext(
                available_intraday_capital=allocation_plan.intraday_capital,
                used_intraday_capital=used_intraday_capital,
                daily_realized_pnl=daily_realized_pnl,
                trades_taken_today=trades_taken_today,
                max_daily_loss=allocation_plan.max_daily_loss,
                max_trades_per_day=allocation_plan.max_trades_per_day,
            )

            decision = risk_manager.evaluate(order, risk_context)
            print(f"  -> Risk decision: approved={decision.approved} reason={decision.reason}")

            if not decision.approved:
                print("  -> Trade blocked by risk manager")
                continue

            placed = broker.place_order(order)
            trades_taken_today += 1
            used_intraday_capital += (placed.average_price or 0.0) * placed.quantity

            print(
                f"  -> Order placed: id={placed.order_id} "
                f"status={placed.status.value} "
                f"avg_price={placed.average_price}"
            )

        history.append(bar)

    print()
    print("=== FINAL POSITIONS ===")
    positions = broker.list_positions()
    if not positions:
        print("No positions")
    else:
        for p in positions:
            print(
                f"{p.exchange}:{p.symbol} | qty={p.quantity} | "
                f"avg={p.average_price} | product={p.product_type.value}"
            )

    print()
    print("=== PAPER TRADER END ===")


if __name__ == "__main__":
    main()
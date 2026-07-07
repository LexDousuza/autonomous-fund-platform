from core.broker.base import OrderRequest, OrderSide, OrderType, ProductType
from core.risk.base import RiskContext
from core.risk.simple_intraday import SimpleIntradayRiskManager


def main() -> None:
    risk_manager = SimpleIntradayRiskManager()

    order = OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY,
        price=2500.0,
        strategy_name="opening_range_breakout",
    )

    context = RiskContext(
        available_intraday_capital=60000.0,
        used_intraday_capital=10000.0,
        daily_realized_pnl=-500.0,
        trades_taken_today=2,
        max_daily_loss=2500.0,
        max_trades_per_day=6,
    )

    decision = risk_manager.evaluate(order, context)

    print("Risk decision")
    print(f"Approved: {decision.approved}")
    print(f"Reason: {decision.reason}")
    print(f"Metadata: {decision.metadata}")


if __name__ == "__main__":
    main()
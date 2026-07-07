from core.broker.base import OrderRequest, OrderSide, OrderType, ProductType
from core.broker.paper import PaperBroker


def main() -> None:
    broker = PaperBroker()

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

    placed = broker.place_order(order)

    print("Order result")
    print(f"Order ID: {placed.order_id}")
    print(f"Status: {placed.status}")
    print(f"Average price: {placed.average_price}")

    print("\nPositions")
    for position in broker.list_positions():
        print(
            f"{position.exchange}:{position.symbol} | "
            f"qty={position.quantity} | avg={position.average_price}"
        )


if __name__ == "__main__":
    main()
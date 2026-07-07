from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from core.broker.base import (
    Broker,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Position,
)


class PaperBroker(Broker):
    """
    Simple in-memory paper broker for V1 testing.

    Current behavior:
    - accepts orders
    - fills them immediately
    - stores order history
    - updates positions in memory
    """

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._positions: dict[tuple[str, str], Position] = {}

    def place_order(self, order_request: OrderRequest) -> Order:
        now = datetime.now()
        order_id = str(uuid4())

        fill_price = order_request.price or 0.0

        order = Order(
            order_id=order_id,
            symbol=order_request.symbol,
            exchange=order_request.exchange,
            side=order_request.side,
            quantity=order_request.quantity,
            filled_quantity=order_request.quantity,
            status=OrderStatus.FILLED,
            order_type=order_request.order_type,
            product_type=order_request.product_type,
            created_at=now,
            updated_at=now,
            average_price=fill_price,
            raw={
                "paper_fill": True,
                "strategy_name": order_request.strategy_name,
                "metadata": order_request.metadata,
            },
        )

        self._orders[order_id] = order
        self._apply_fill_to_positions(order)

        return order

    def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order is None:
            return False

        if order.status == OrderStatus.FILLED:
            return False

        self._orders[order_id] = replace(
            order,
            status=OrderStatus.CANCELLED,
            updated_at=datetime.now(),
        )
        return True

    def get_order(self, order_id: str) -> Order:
        if order_id not in self._orders:
            raise ValueError(f"Unknown order_id: {order_id}")
        return self._orders[order_id]

    def list_positions(self) -> list[Position]:
        return list(self._positions.values())

    def _apply_fill_to_positions(self, order: Order) -> None:
        key = (order.symbol, order.exchange)
        existing = self._positions.get(key)

        signed_qty = order.quantity if order.side == OrderSide.BUY else -order.quantity
        fill_price = order.average_price or 0.0

        if existing is None:
            self._positions[key] = Position(
                symbol=order.symbol,
                exchange=order.exchange,
                quantity=signed_qty,
                average_price=fill_price,
                product_type=order.product_type,
                last_price=fill_price,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
            )
            return

        new_quantity = existing.quantity + signed_qty

        # If position goes flat, keep avg price simple and zero out quantity
        if new_quantity == 0:
            self._positions[key] = Position(
                symbol=existing.symbol,
                exchange=existing.exchange,
                quantity=0,
                average_price=existing.average_price,
                product_type=existing.product_type,
                last_price=fill_price,
                unrealized_pnl=0.0,
                realized_pnl=existing.realized_pnl,
            )
            return

        # Simple weighted-average logic for same-direction accumulation.
        # This is intentionally basic for V1 paper mode.
        if (existing.quantity >= 0 and signed_qty > 0) or (existing.quantity <= 0 and signed_qty < 0):
            total_abs_qty = abs(existing.quantity) + abs(signed_qty)
            weighted_avg = (
                (abs(existing.quantity) * existing.average_price)
                + (abs(signed_qty) * fill_price)
            ) / total_abs_qty

            self._positions[key] = Position(
                symbol=existing.symbol,
                exchange=existing.exchange,
                quantity=new_quantity,
                average_price=weighted_avg,
                product_type=existing.product_type,
                last_price=fill_price,
                unrealized_pnl=existing.unrealized_pnl,
                realized_pnl=existing.realized_pnl,
            )
            return

        # Opposite-direction trade that reduces/reverses a position:
        # keep logic simple for now and update quantity + last price.
        self._positions[key] = Position(
            symbol=existing.symbol,
            exchange=existing.exchange,
            quantity=new_quantity,
            average_price=existing.average_price if new_quantity != 0 else fill_price,
            product_type=existing.product_type,
            last_price=fill_price,
            unrealized_pnl=existing.unrealized_pnl,
            realized_pnl=existing.realized_pnl,
        )
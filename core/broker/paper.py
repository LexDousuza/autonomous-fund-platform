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
    - fills them immediately, at the requested price adjusted by
      ``slippage_bps`` (0.0 by default - exact requested-price fills,
      the original V1 behavior) against the trader's favor: worse for
      BUY, worse for SELL. This is a deliberately simple, deterministic
      cost model (a fixed adjustment, not simulated order-book depth or
      partial fills) - real slippage also depends on order size vs.
      liquidity, which this does not model. Zero by default so nothing
      that already constructs ``PaperBroker()`` changes behavior; the
      backtester passes a non-zero value (see
      ``core/backtest/day_runner.py``) so backtest results aren't
      inflated by assuming perfect fills.
    - stores order history
    - updates positions in memory
    - is idempotent on ``OrderRequest.client_order_id`` when set (see
      ``core/execution/idempotency.py``): a repeated ``place_order()``
      call with the same ``client_order_id`` returns the original fill
      instead of creating a second one and double-applying it to
      positions - this is what a real broker enforcing order-reference-id
      deduplication would look like from the caller's side, and lets that
      guarantee be exercised in tests without a real Groww account.
    """

    def __init__(self, slippage_bps: float = 0.0) -> None:
        self._orders: dict[str, Order] = {}
        self._positions: dict[tuple[str, str], Position] = {}
        self._orders_by_client_id: dict[str, str] = {}
        self._slippage_bps = slippage_bps

    def place_order(self, order_request: OrderRequest) -> Order:
        if order_request.client_order_id is not None:
            existing_order_id = self._orders_by_client_id.get(order_request.client_order_id)
            if existing_order_id is not None:
                return self._orders[existing_order_id]

        now = datetime.now()
        order_id = str(uuid4())

        fill_price = self._apply_slippage(order_request.price or 0.0, order_request.side)

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
            strike=order_request.strike,
            expiry=order_request.expiry,
            option_type=order_request.option_type,
            lot_size=order_request.lot_size,
            position_effect=order_request.position_effect,
        )

        self._orders[order_id] = order
        self._apply_fill_to_positions(order)
        if order_request.client_order_id is not None:
            self._orders_by_client_id[order_request.client_order_id] = order_id

        return order

    def _apply_slippage(self, requested_price: float, side: OrderSide) -> float:
        """Move ``requested_price`` against the trader by ``self._slippage_bps``: up for BUY, down for SELL."""
        if requested_price <= 0 or self._slippage_bps == 0:
            return requested_price
        adjustment = requested_price * (self._slippage_bps / 10_000)
        return requested_price + adjustment if side == OrderSide.BUY else requested_price - adjustment

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
                strike=order.strike,
                expiry=order.expiry,
                option_type=order.option_type,
                lot_size=order.lot_size,
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
                strike=existing.strike,
                expiry=existing.expiry,
                option_type=existing.option_type,
                lot_size=existing.lot_size,
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
                strike=existing.strike,
                expiry=existing.expiry,
                option_type=existing.option_type,
                lot_size=existing.lot_size,
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
            strike=existing.strike,
            expiry=existing.expiry,
            option_type=existing.option_type,
            lot_size=existing.lot_size,
        )
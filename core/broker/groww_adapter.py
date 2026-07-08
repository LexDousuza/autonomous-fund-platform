"""
Groww broker adapter V1.

Translates between this platform's broker-independent models
(``OrderRequest``/``Order``/``Position`` in ``core/broker/base.py``) and
Groww's wire shapes (``core/broker/groww_models.py``). All Groww-specific
parsing/status vocabulary lives here (or in ``groww_models.py``) and never
leaks into application code.

Outbound HTTP calls are delegated to ``GrowwHttpTransport``
(``core/broker/groww_transport.py``): ``_send_place_order``,
``_send_cancel_order``, ``_fetch_order``, and ``_fetch_positions`` build a
request payload, call the transport, and parse its raw ``dict`` response
into a ``groww_models`` dataclass via that dataclass's ``from_*_json``
classmethod. The adapter never touches ``urllib``/HTTP directly - that
stays entirely inside the transport - and the transport never touches
``Order``/``Position`` - that stays entirely inside the adapter/models.
Unit tests can substitute a fake transport (or monkeypatch these seam
methods directly, as the pre-existing tests do) without any live Groww
API call.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.broker.base import (
    Broker,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ProductType,
)
from core.broker.groww_models import (
    GrowwCancelResponse,
    GrowwCredentials,
    GrowwOrderRequest,
    GrowwOrderResponse,
    GrowwPosition,
)
from core.broker.groww_transport import GrowwHttpTransport, GrowwTransportConfig


class GrowwBrokerAdapter(Broker):
    """
    ``Broker`` implementation backed by the Groww API.

    Implements the same interface as ``PaperBroker`` so the execution
    orchestrator can use either without any application-level branching.

    ``mode`` distinguishes Groww's own paper-trading mode from live
    trading with real capital (``"paper"``/``"live"``). Groww is not
    known (as of this task) to expose a genuinely separate sandbox API,
    so both modes currently route through the same transport/base_url -
    "paper" here means "the caller configured a non-production base_url
    and/or credentials", not "this adapter simulates fills locally". If
    Groww is later confirmed to have a real sandbox endpoint, that base
    URL selection belongs in the broker factory (which already builds
    ``base_url`` from ``BrokerConfig.metadata``), not a silent branch
    inside this adapter.

    ``LIVE_TRANSPORT_IMPLEMENTED`` is a capability flag, not a runtime
    state: it lets callers that care whether real order placement is
    possible (e.g. the live trader app, before starting a session) check
    without needing to know which methods are still stubs. Real outbound
    calls are now wired through ``GrowwHttpTransport``, so this is
    ``True`` - but the transport's endpoint paths/payload shapes/auth
    header scheme are still unverified assumptions (see
    ``groww_transport.py``'s module docstring), not confirmed against
    real Groww API documentation.
    """

    LIVE_TRANSPORT_IMPLEMENTED = True

    def __init__(
        self,
        credentials: GrowwCredentials,
        base_url: str = "",
        mode: str = "paper",
        timeout_seconds: float = 10.0,
        transport: GrowwHttpTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._base_url = base_url
        self.mode = mode
        self._transport = transport or GrowwHttpTransport(
            GrowwTransportConfig(
                base_url=base_url,
                credentials=credentials,
                timeout_seconds=timeout_seconds,
            )
        )

    def place_order(self, order_request: OrderRequest) -> Order:
        """Build a Groww order payload, send it, and map the response back."""
        groww_request = self._build_groww_order_request(order_request)
        response = self._send_place_order(groww_request)
        return self._map_order_response(order_request, response)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order and report whether Groww confirmed the cancellation."""
        response = self._send_cancel_order(order_id)
        return self._map_status(response.status) == OrderStatus.CANCELLED

    def get_order(self, order_id: str) -> Order:
        """Fetch the current state of a previously placed order."""
        response = self._fetch_order(order_id)
        return self._map_fetched_order(response)

    def list_positions(self) -> list[Position]:
        """Fetch and map all current Groww positions."""
        return [self._map_position(position) for position in self._fetch_positions()]

    # -- request building -------------------------------------------------

    def _build_groww_order_request(self, order: OrderRequest) -> GrowwOrderRequest:
        return GrowwOrderRequest(
            exchange=order.exchange,
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            order_type=order.order_type.value,
            product_type=order.product_type.value,
            price=order.price,
            trigger_price=order.trigger_price,
        )

    # -- outbound calls (delegate to the transport; mockable in tests) ------

    def _send_place_order(self, request: GrowwOrderRequest) -> GrowwOrderResponse:
        payload = {
            "exchange": request.exchange,
            "symbol": request.symbol,
            "side": request.side,
            "quantity": request.quantity,
            "order_type": request.order_type,
            "product_type": request.product_type,
            "price": request.price,
            "trigger_price": request.trigger_price,
        }
        data = self._transport.place_order(payload)
        return GrowwOrderResponse.from_place_order_json(data)

    def _send_cancel_order(self, order_id: str) -> GrowwCancelResponse:
        data = self._transport.cancel_order(order_id)
        return GrowwCancelResponse.from_json(data)

    def _fetch_order(self, order_id: str) -> GrowwOrderResponse:
        data = self._transport.get_order(order_id)
        return GrowwOrderResponse.from_get_order_json(data)

    def _fetch_positions(self) -> list[GrowwPosition]:
        data = self._transport.list_positions()
        return [GrowwPosition.from_json(item) for item in data.get("positions", [])]

    # -- response mapping ---------------------------------------------------

    def _map_order_response(
        self,
        order_request: OrderRequest,
        response: GrowwOrderResponse,
    ) -> Order:
        """
        Map a placement response into an ``Order``.

        Symbol/side/quantity/etc. come from ``order_request`` (already
        known - this is the order we just sent), not ``response``, since a
        placement response only needs to carry order-id/status/fill data.
        """
        now = datetime.now(UTC)

        return Order(
            order_id=response.order_id,
            symbol=order_request.symbol,
            exchange=order_request.exchange,
            side=order_request.side,
            quantity=order_request.quantity,
            filled_quantity=response.filled_quantity,
            status=self._map_status(response.status),
            order_type=order_request.order_type,
            product_type=order_request.product_type,
            created_at=now,
            updated_at=now,
            average_price=response.average_price,
            raw=response.raw_response or {},
        )

    def _map_fetched_order(self, response: GrowwOrderResponse) -> Order:
        """
        Map a fetched order (``get_order``) into an ``Order``.

        Unlike ``_map_order_response``, there is no original
        ``OrderRequest`` to draw from, so every field is read off
        ``response`` itself. A fetched order is expected to always carry
        ``symbol``/``exchange``/``side``/``quantity``/``order_type``/
        ``product_type``; if Groww omits one, converting ``None`` to the
        corresponding enum raises immediately rather than silently
        defaulting to a possibly-wrong value.
        """
        now = datetime.now(UTC)

        return Order(
            order_id=response.order_id,
            symbol=response.symbol,
            exchange=response.exchange,
            side=OrderSide(response.side),
            quantity=response.quantity,
            filled_quantity=response.filled_quantity,
            status=self._map_status(response.status),
            order_type=OrderType(response.order_type),
            product_type=ProductType(response.product_type),
            created_at=now,
            updated_at=now,
            average_price=response.average_price,
            raw=response.raw_response or {},
        )

    def _map_position(self, position: GrowwPosition) -> Position:
        return Position(
            symbol=position.symbol,
            exchange=position.exchange,
            quantity=position.quantity,
            average_price=position.average_price,
            product_type=ProductType(position.product_type),
            last_price=position.last_price,
            unrealized_pnl=position.unrealized_pnl,
            realized_pnl=position.realized_pnl,
        )

    def _map_status(self, broker_status: str) -> OrderStatus:
        normalized = broker_status.strip().upper()

        if normalized in {"FILLED", "EXECUTED"}:
            return OrderStatus.FILLED
        if normalized == "REJECTED":
            return OrderStatus.REJECTED
        if normalized in {"CANCELLED", "CANCELED"}:
            return OrderStatus.CANCELLED
        if normalized in {"PARTIAL", "PARTIALLY_FILLED"}:
            return OrderStatus.PARTIALLY_FILLED
        if normalized in {"OPEN", "TRIGGER_PENDING"}:
            return OrderStatus.OPEN
        if normalized in {"PENDING", "VALIDATION_PENDING", "NEW"}:
            return OrderStatus.PENDING

        return OrderStatus.PENDING

"""
Groww broker adapter V1.

Translates between this platform's broker-independent models
(``OrderRequest``/``Order``/``Position`` in ``core/broker/base.py``) and
Groww's wire shapes (``core/broker/groww_models.py``). All Groww-specific
parsing/status vocabulary lives here (or in ``groww_models.py``) and never
leaks into application code.

Outbound calls are delegated to ``GrowwHttpTransport``
(``core/broker/groww_transport.py``, which itself wraps the official
``growwapi`` SDK): ``_send_place_order``, ``_send_cancel_order``,
``_fetch_order``, and ``_fetch_positions`` build a request payload, call
the transport, and parse its raw ``dict`` response into a
``groww_models`` dataclass via that dataclass's ``from_*_json``
classmethod. The adapter never touches the SDK directly - that stays
entirely inside the transport - and the transport never touches
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

# V1 default: this platform doesn't expose order validity as a per-order
# concept, so every order is placed DAY-validity (verified constant value:
# see https://groww.in/trade-api/docs/python-sdk/annexures).
_VALIDITY_DAY = "DAY"

# Our ProductType <-> Groww's raw product code. Groww's real codes ("MIS",
# "CNC") are verified from the SDK's own constants
# (GrowwAPI.PRODUCT_MIS/PRODUCT_CNC), not guessed.
_PRODUCT_TYPE_TO_GROWW = {ProductType.INTRADAY: "MIS", ProductType.DELIVERY: "CNC"}
_GROWW_TO_PRODUCT_TYPE = {"MIS": ProductType.INTRADAY, "CNC": ProductType.DELIVERY, "NRML": ProductType.DELIVERY}

# Our OrderType <-> Groww's order_type. MARKET/LIMIT share the same string
# in both vocabularies; only STOP has no direct Groww equivalent - SL
# (stop-loss limit) is the closest single mapping, since V1's exit engine
# never actually places a STOP order today (see core/execution/orchestrator.py).
_ORDER_TYPE_TO_GROWW = {OrderType.MARKET: "MARKET", OrderType.LIMIT: "LIMIT", OrderType.STOP: "SL"}

# Groww's full, verified order_status vocabulary
# (https://groww.in/trade-api/docs/python-sdk/annexures) mapped onto this
# platform's smaller OrderStatus enum.
_STATUS_TO_ORDER_STATUS = {
    "NEW": OrderStatus.PENDING,
    "ACKED": OrderStatus.OPEN,
    "TRIGGER_PENDING": OrderStatus.OPEN,
    "APPROVED": OrderStatus.OPEN,
    "MODIFICATION_REQUESTED": OrderStatus.OPEN,
    "CANCELLATION_REQUESTED": OrderStatus.OPEN,
    "EXECUTED": OrderStatus.FILLED,
    "COMPLETED": OrderStatus.FILLED,
    "DELIVERY_AWAITED": OrderStatus.FILLED,
    "REJECTED": OrderStatus.REJECTED,
    "FAILED": OrderStatus.REJECTED,
    "CANCELLED": OrderStatus.CANCELLED,
}


class GrowwBrokerAdapter(Broker):
    """
    ``Broker`` implementation backed by the official ``growwapi`` SDK
    (via ``GrowwHttpTransport``).

    Implements the same interface as ``PaperBroker`` so the execution
    orchestrator can use either without any application-level branching.

    ``mode`` distinguishes Groww's own paper-trading mode from live
    trading with real capital (``"paper"``/``"live"``). Groww is not
    known to expose a genuinely separate sandbox API, so both modes
    currently authenticate identically - "paper" here means "the caller
    configured non-production credentials", not "this adapter simulates
    fills locally".

    ``LIVE_TRANSPORT_IMPLEMENTED`` is a capability flag, not a runtime
    state: it lets callers that care whether real order placement is
    possible (e.g. the live trader app, before starting a session) check
    without needing to know which methods are still stubs. Real outbound
    calls go through the official ``growwapi`` SDK (see
    ``groww_transport.py``), with request/response field names verified
    against Groww's official docs - the auth flow, order lifecycle, and
    positions shape are no longer guesses, though the exact response
    shape for every edge case (e.g. partial fills, AMO orders) has not
    been exercised against a real account.
    """

    LIVE_TRANSPORT_IMPLEMENTED = True

    def __init__(
        self,
        credentials: GrowwCredentials,
        mode: str = "paper",
        timeout_seconds: float = 10.0,
        transport: GrowwHttpTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self.mode = mode
        self._transport = transport or GrowwHttpTransport(
            GrowwTransportConfig(credentials=credentials, timeout_seconds=timeout_seconds)
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
        """Fetch and map all current Groww cash-segment positions."""
        return [self._map_position(position) for position in self._fetch_positions()]

    # -- request building -------------------------------------------------

    def _build_groww_order_request(self, order: OrderRequest) -> GrowwOrderRequest:
        return GrowwOrderRequest(
            exchange=order.exchange,
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            order_type=_ORDER_TYPE_TO_GROWW[order.order_type],
            product_type=_PRODUCT_TYPE_TO_GROWW[order.product_type],
            validity=_VALIDITY_DAY,
            price=order.price,
            trigger_price=order.trigger_price,
            order_reference_id=order.client_order_id,
        )

    # -- outbound calls (delegate to the transport; mockable in tests) ------

    def _send_place_order(self, request: GrowwOrderRequest) -> GrowwOrderResponse:
        payload = {
            "exchange": request.exchange,
            "trading_symbol": request.symbol,
            "transaction_type": request.side,
            "quantity": request.quantity,
            "order_type": request.order_type,
            "product": request.product_type,
            "validity": request.validity,
            "price": request.price,
            "trigger_price": request.trigger_price,
        }
        if request.order_reference_id is not None:
            payload["order_reference_id"] = request.order_reference_id
        data = self._transport.place_order(payload)
        return GrowwOrderResponse.from_place_order_json(data)

    def _send_cancel_order(self, order_id: str) -> GrowwCancelResponse:
        data = self._transport.cancel_order(order_id)
        return GrowwCancelResponse.from_json(data)

    def _fetch_order(self, order_id: str) -> GrowwOrderResponse:
        data = self._transport.get_order(order_id)
        return GrowwOrderResponse.from_order_detail_json(data)

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
        placement response only carries order-id/status/fill data.
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
        Map a fetched order (``get_order`` -> ``get_order_detail``) into
        an ``Order``.

        Unlike ``_map_order_response``, there is no original
        ``OrderRequest`` to draw from, so every field is read off
        ``response`` itself - a full order-detail fetch always carries
        symbol/exchange/side/quantity/order_type/product_type; if Groww
        omits one, converting ``None`` raises immediately rather than
        silently defaulting to a possibly-wrong value.

        Groww's ``order_status`` has no distinct "partially filled"
        value - an EXECUTED/COMPLETED status with ``filled_quantity``
        short of the full ``quantity`` is inferred as a partial fill,
        since both fields are independently verified as real.
        """
        now = datetime.now(UTC)
        status = self._map_status(response.status)
        if status == OrderStatus.FILLED and response.filled_quantity < (response.quantity or 0):
            status = OrderStatus.PARTIALLY_FILLED

        return Order(
            order_id=response.order_id,
            symbol=response.symbol,
            exchange=response.exchange,
            side=OrderSide(response.side),
            quantity=response.quantity,
            filled_quantity=response.filled_quantity,
            status=status,
            order_type=self._map_order_type_from_groww(response.order_type),
            product_type=_GROWW_TO_PRODUCT_TYPE.get(response.product_type, ProductType.INTRADAY),
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
            product_type=_GROWW_TO_PRODUCT_TYPE.get(position.product_type, ProductType.INTRADAY),
            last_price=position.last_price,
            unrealized_pnl=position.unrealized_pnl,
            realized_pnl=position.realized_pnl,
        )

    @staticmethod
    def _map_order_type_from_groww(groww_order_type: str) -> OrderType:
        reverse = {value: key for key, value in _ORDER_TYPE_TO_GROWW.items()}
        # SL_M (stop-loss market) has no inbound V1 equivalent beyond STOP.
        reverse.setdefault("SL_M", OrderType.STOP)
        return reverse.get(groww_order_type, OrderType.MARKET)

    def _map_status(self, groww_status: str) -> OrderStatus:
        return _STATUS_TO_ORDER_STATUS.get(groww_status.strip().upper(), OrderStatus.PENDING)

"""
Kite (Zerodha Kite Connect) broker adapter V1.

Translates between this platform's broker-independent models
(``OrderRequest``/``Order``/``Position`` in ``core/broker/base.py``) and
Kite's wire shapes (``core/broker/kite_models.py``). All Kite-specific
parsing/status vocabulary lives here (or in ``kite_models.py``) and never
leaks into application code.

Outbound calls are delegated to ``KiteHttpTransport``
(``core/broker/kite_transport.py``, which itself wraps the official
``kiteconnect`` SDK): ``_send_place_order``, ``_send_cancel_order``,
``_fetch_order``, and ``_fetch_positions`` build a request payload, call
the transport, and parse its raw ``dict`` response via ``kite_models``.
Unit tests can substitute a fake transport without any live Kite API
call - mirrors ``groww_adapter.py``'s shape exactly.
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
from core.broker.kite_models import (
    KiteCancelResponse,
    KiteCredentials,
    KiteOrderRequest,
    KiteOrderResponse,
    KitePosition,
)
from core.broker.kite_transport import KiteHttpTransport, KiteTransportConfig

# V1 default: this platform doesn't expose order validity/variety as
# per-order concepts, so every order is a regular, DAY-validity order
# (verified constants: https://kite.trade/docs/connect/v3/orders/).
_VALIDITY_DAY = "DAY"
_VARIETY_REGULAR = "regular"

# Our ProductType <-> Kite's real product codes. Verified against
# https://kite.trade/docs/connect/v3/orders/ - MIS/CNC are the same
# literal codes Groww uses, both being standard NSE product vocabulary.
_PRODUCT_TYPE_TO_KITE = {ProductType.INTRADAY: "MIS", ProductType.DELIVERY: "CNC"}
_KITE_TO_PRODUCT_TYPE = {
    "MIS": ProductType.INTRADAY,
    "CNC": ProductType.DELIVERY,
    "NRML": ProductType.DELIVERY,
    "MTF": ProductType.DELIVERY,
}

# Our OrderType <-> Kite's order_type. MARKET/LIMIT share the same string
# in both vocabularies; only STOP has no direct Kite equivalent - SL
# (stop-loss limit) is the closest single mapping, matching
# groww_adapter.py's identical reasoning (V1's exit engine never places a
# true STOP order today).
_ORDER_TYPE_TO_KITE = {OrderType.MARKET: "MARKET", OrderType.LIMIT: "LIMIT", OrderType.STOP: "SL"}

# Kite's full, verified order status vocabulary
# (https://kite.trade/docs/connect/v3/orders/) mapped onto this
# platform's smaller OrderStatus enum. Every "*PENDING*"/"*RECEIVED*"
# interim state maps to PENDING except TRIGGER PENDING, which - like
# Groww's equivalent - means an accepted order waiting on its trigger,
# closer to OPEN than to not-yet-accepted.
_STATUS_TO_ORDER_STATUS = {
    "COMPLETE": OrderStatus.FILLED,
    "OPEN": OrderStatus.OPEN,
    "TRIGGER PENDING": OrderStatus.OPEN,
    "REJECTED": OrderStatus.REJECTED,
    "CANCELLED": OrderStatus.CANCELLED,
    "PUT ORDER REQ RECEIVED": OrderStatus.PENDING,
    "VALIDATION PENDING": OrderStatus.PENDING,
    "OPEN PENDING": OrderStatus.PENDING,
    "MODIFY VALIDATION PENDING": OrderStatus.PENDING,
    "MODIFY PENDING": OrderStatus.PENDING,
    "CANCEL PENDING": OrderStatus.PENDING,
    "AMO REQ RECEIVED": OrderStatus.PENDING,
}


class KiteBrokerAdapter(Broker):
    """
    ``Broker`` implementation backed by the official ``kiteconnect`` SDK
    (via ``KiteHttpTransport``).

    Implements the same interface as ``PaperBroker``/``GrowwBrokerAdapter``
    so the execution orchestrator can use any of them without any
    application-level branching.

    ``mode`` distinguishes "the operator configured this deliberately" from
    live trading with real capital (``"paper"``/``"live"``) - same as
    ``GrowwBrokerAdapter``. Kite Connect has no separate sandbox/paper
    trading API at all (it only ever trades a real Zerodha account), so
    "paper" here is purely a descriptive intent flag, never a runtime
    branch inside this adapter - the actual safety boundary is enforced
    one layer up, in ``apps/live_trader``, which only ever constructs this
    adapter with ``mode="live"`` (see that app's ``LiveTraderConfig``).

    ``LIVE_TRANSPORT_IMPLEMENTED`` is a capability flag, not a runtime
    state - see ``GrowwBrokerAdapter``'s docstring for why it exists.
    Real outbound calls go through the official ``kiteconnect`` SDK (see
    ``kite_transport.py``), with every field name verified against Kite's
    official docs and the installed SDK's own source - though, like
    Groww, the exact response shape for every edge case (AMO orders,
    partial fills across multiple exchanges, etc.) has not been exercised
    against a real account.
    """

    LIVE_TRANSPORT_IMPLEMENTED = True

    def __init__(
        self,
        credentials: KiteCredentials,
        mode: str = "paper",
        timeout_seconds: float = 10.0,
        transport: KiteHttpTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self.mode = mode
        self._transport = transport or KiteHttpTransport(
            KiteTransportConfig(credentials=credentials, timeout_seconds=timeout_seconds)
        )

    def place_order(self, order_request: OrderRequest) -> Order:
        """
        Build a Kite order payload, send it, then immediately fetch the
        order's real post-placement state.

        Unlike Groww (whose placement response already carries a
        status), Kite's ``place_order`` returns only an ``order_id`` (see
        ``kite_models.py``'s module docstring) - so a second call
        (``order_history``) is always required here to learn whether the
        order is open, rejected, or already filled.
        """
        kite_request = self._build_kite_order_request(order_request)
        placement = self._send_place_order(kite_request)
        detail = self._fetch_order(placement.order_id)
        return self._map_fetched_order(detail)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order and report whether Kite confirmed the cancellation."""
        response = self._send_cancel_order(order_id)
        return self._map_status(response.status) == OrderStatus.CANCELLED

    def get_order(self, order_id: str) -> Order:
        """Fetch the current state of a previously placed order."""
        response = self._fetch_order(order_id)
        return self._map_fetched_order(response)

    def list_positions(self) -> list[Position]:
        """Fetch and map all current net positions."""
        return [self._map_position(position) for position in self._fetch_positions()]

    # -- request building -------------------------------------------------

    def _build_kite_order_request(self, order: OrderRequest) -> KiteOrderRequest:
        return KiteOrderRequest(
            exchange=order.exchange,
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            order_type=_ORDER_TYPE_TO_KITE[order.order_type],
            product_type=_PRODUCT_TYPE_TO_KITE[order.product_type],
            validity=_VALIDITY_DAY,
            variety=_VARIETY_REGULAR,
            price=order.price,
            trigger_price=order.trigger_price,
            tag=order.client_order_id,
        )

    # -- outbound calls (delegate to the transport; mockable in tests) ------

    def _send_place_order(self, request: KiteOrderRequest) -> KiteOrderResponse:
        payload = {
            "exchange": request.exchange,
            "tradingsymbol": request.symbol,
            "transaction_type": request.side,
            "quantity": request.quantity,
            "order_type": request.order_type,
            "product": request.product_type,
            "validity": request.validity,
            "price": request.price,
            "trigger_price": request.trigger_price,
        }
        if request.tag is not None:
            payload["tag"] = request.tag
        data = self._transport.place_order(payload)
        return KiteOrderResponse(order_id=data["order_id"], status="PUT ORDER REQ RECEIVED")

    def _send_cancel_order(self, order_id: str) -> KiteCancelResponse:
        data = self._transport.cancel_order(order_id)
        return KiteCancelResponse(order_id=data["order_id"])

    def _fetch_order(self, order_id: str) -> KiteOrderResponse:
        data = self._transport.get_order(order_id)
        return KiteOrderResponse.from_order_detail_json(data)

    def _fetch_positions(self) -> list[KitePosition]:
        data = self._transport.list_positions()
        return [KitePosition.from_json(item) for item in data.get("net", [])]

    # -- response mapping ---------------------------------------------------

    def _map_fetched_order(self, response: KiteOrderResponse) -> Order:
        """
        Map a fetched order (``get_order``/``order_history``) into an
        ``Order``. Every field is always populated here (see
        ``KiteOrderResponse``'s docstring - there is no separate sparse
        placement shape to worry about, unlike Groww).

        Kite's ``status`` has no distinct "partially filled" value either
        - a ``COMPLETE`` status with ``filled_quantity`` short of the
        full ``quantity`` is inferred as a partial fill, mirroring
        ``GrowwBrokerAdapter``'s identical reasoning.
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
            order_type=self._map_order_type_from_kite(response.order_type),
            product_type=_KITE_TO_PRODUCT_TYPE.get(response.product_type, ProductType.INTRADAY),
            created_at=now,
            updated_at=now,
            average_price=response.average_price,
            raw=response.raw_response or {},
        )

    def _map_position(self, position: KitePosition) -> Position:
        return Position(
            symbol=position.symbol,
            exchange=position.exchange,
            quantity=position.quantity,
            average_price=position.average_price,
            product_type=_KITE_TO_PRODUCT_TYPE.get(position.product_type, ProductType.INTRADAY),
            last_price=position.last_price,
            unrealized_pnl=position.unrealized_pnl,
            realized_pnl=position.realized_pnl,
        )

    @staticmethod
    def _map_order_type_from_kite(kite_order_type: str) -> OrderType:
        reverse = {value: key for key, value in _ORDER_TYPE_TO_KITE.items()}
        # SL-M (stop-loss market) has no inbound V1 equivalent beyond STOP.
        reverse.setdefault("SL-M", OrderType.STOP)
        return reverse.get(kite_order_type, OrderType.MARKET)

    def _map_status(self, kite_status: str) -> OrderStatus:
        return _STATUS_TO_ORDER_STATUS.get(kite_status.strip().upper(), OrderStatus.PENDING)

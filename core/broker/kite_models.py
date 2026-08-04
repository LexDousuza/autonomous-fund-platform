"""
Kite (Zerodha Kite Connect)-specific wire-shape models.

These dataclasses mirror what the Kite Connect API sends/receives and
never leak outside ``core/broker/kite_adapter.py``. Application code only
ever sees the broker-independent models in ``core/broker/base.py``
(``OrderRequest``, ``Order``, ``Position``); ``KiteBrokerAdapter`` is the
sole translator between the two.

Field names verified against the official ``kiteconnect`` Python SDK
source (https://github.com/zerodha/pykiteconnect) and Kite Connect's
published API docs (https://kite.trade/docs/connect/v3/orders/ and
.../portfolio/), not guessed.

Two real, verified differences from ``groww_models.py`` shaped this
module:

1. ``KiteConnect.place_order()``/``cancel_order()`` return only a bare
   ``order_id`` string - no status at all. There is no
   ``from_place_order_json`` here as a result: ``KiteBrokerAdapter``
   always follows a placement with an immediate ``order_history()``
   fetch to learn the real post-placement status, so every ``Order`` this
   adapter produces goes through ``from_order_detail_json`` - one parser,
   not two.
2. ``KiteConnect.cancel_order()`` raises on failure and returns the
   ``order_id`` on success - it never reports an ambiguous status the way
   Groww's cancel response does. ``KiteCancelResponse`` is therefore never
   parsed from JSON; the adapter builds it directly once the transport
   call returns without raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.broker.kite_errors import KiteTransportError


@dataclass(frozen=True)
class KiteCredentials:
    """
    API credentials for authenticating against the Kite Connect API.

    ``access_token`` is NOT a static secret - it is obtained via Kite's
    browser-based login flow (see ``KiteConnect.login_url()`` /
    ``generate_session()``) and expires at 6 AM IST every day (a
    regulatory requirement, not a bug). This platform never performs that
    login itself: ``scripts/kite_generate_access_token.py`` is a
    separate, manually-run helper the operator runs once each morning to
    obtain the day's ``access_token`` and store it in
    ``.env.live_trader`` - by the time this dataclass is constructed, the
    token already exists.
    """

    api_key: str
    access_token: str


@dataclass(frozen=True)
class KiteOrderRequest:
    """
    Outbound order payload shape, built from an internal ``OrderRequest``.

    ``variety`` and ``validity`` have no equivalent in this platform's
    broker-independent ``OrderRequest`` - ``KiteBrokerAdapter`` fills them
    in with fixed V1 defaults (``VARIETY_REGULAR``/``VALIDITY_DAY``) when
    building this model, since the platform doesn't expose either as a
    per-order concept yet.

    ``tag`` carries ``OrderRequest.algo_id`` (SEBI's regulatory algo tag)
    when set, falling back to ``client_order_id`` (this platform's own
    idempotency key, see ``core/execution/idempotency.py``) otherwise -
    see ``KiteBrokerAdapter._build_kite_order_request``. Kite's own
    free-form order tag field, capped at 20 alphanumeric characters by
    the API. Left ``None`` when the caller didn't set either.
    """

    exchange: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    product_type: str
    validity: str
    variety: str
    price: float | None = None
    trigger_price: float | None = None
    tag: str | None = None


@dataclass(frozen=True)
class KiteOrderResponse:
    """
    Full order state, as returned by ``order_history()`` (the last entry
    in that call's chronological list is the order's current state).

    Unlike ``GrowwOrderResponse``, there is no separate "placement
    response" shape here (see module docstring) - every field below is
    always populated, since this is always parsed from a full
    order-detail fetch.
    """

    order_id: str
    status: str
    average_price: float | None = None
    filled_quantity: int = 0
    symbol: str | None = None
    exchange: str | None = None
    side: str | None = None
    order_type: str | None = None
    product_type: str | None = None
    quantity: int | None = None
    raw_response: dict[str, Any] | None = None

    @classmethod
    def from_order_detail_json(cls, data: dict[str, Any]) -> "KiteOrderResponse":
        """
        Parse one order-history entry (also the shape ``orders()``
        returns per order).

        Verified shape (https://kite.trade/docs/connect/v3/orders/):
        ``{"order_id", "status", "tradingsymbol", "exchange",
        "transaction_type", "order_type", "product", "quantity",
        "filled_quantity", "average_price", ...}``.
        """
        try:
            average_price = data.get("average_price")
            return cls(
                order_id=data["order_id"],
                status=data["status"],
                average_price=float(average_price) if average_price else None,
                filled_quantity=int(data.get("filled_quantity", 0)),
                symbol=data["tradingsymbol"],
                exchange=data["exchange"],
                side=data["transaction_type"],
                order_type=data["order_type"],
                product_type=data["product"],
                quantity=int(data["quantity"]),
                raw_response=data,
            )
        except KeyError as exc:
            raise KiteTransportError(
                f"Malformed Kite order-detail response, missing field: {exc}"
            ) from exc


@dataclass(frozen=True)
class KiteCancelResponse:
    """
    Outcome of a cancel request.

    Built directly by ``KiteBrokerAdapter`` (see module docstring) - Kite
    itself only ever confirms a cancel by returning the ``order_id``
    without raising, so ``status`` is always ``"CANCELLED"`` here; a
    failed cancel surfaces as a raised ``KiteApiError`` instead of a
    parsed failure status.
    """

    order_id: str
    status: str = "CANCELLED"


@dataclass(frozen=True)
class KitePosition:
    """A single open position as returned by ``positions()``'s ``"net"`` list."""

    symbol: str
    exchange: str
    quantity: int
    average_price: float
    product_type: str
    last_price: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    raw_response: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "KitePosition":
        """
        Parse a single position entry.

        Verified shape (https://kite.trade/docs/connect/v3/portfolio/):
        ``{"tradingsymbol", "exchange", "quantity", "average_price",
        "last_price", "unrealised", "realised", "product", ...}`` - unlike
        Groww, Kite's positions response carries live ``last_price``/
        ``unrealised`` directly, so both are populated here instead of
        left ``None``.
        """
        try:
            return cls(
                symbol=data["tradingsymbol"],
                exchange=data["exchange"],
                quantity=int(data["quantity"]),
                average_price=float(data["average_price"]),
                product_type=data["product"],
                last_price=data.get("last_price"),
                unrealized_pnl=data.get("unrealised"),
                realized_pnl=data.get("realised"),
                raw_response=data,
            )
        except KeyError as exc:
            raise KiteTransportError(
                f"Malformed Kite position payload, missing field: {exc}"
            ) from exc

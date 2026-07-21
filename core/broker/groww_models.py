"""
Groww-specific wire-shape models.

These dataclasses mirror what the Groww API sends/receives and never leak
outside ``core/broker/groww_adapter.py``. Application code only ever sees
the broker-independent models in ``core/broker/base.py`` (``OrderRequest``,
``Order``, ``Position``); ``GrowwBrokerAdapter`` is the sole translator
between the two.

The ``from_*_json`` classmethods below parse the raw ``dict`` payloads
returned by ``GrowwHttpTransport`` (which itself wraps the official
``growwapi`` SDK - see that module's docstring) into these dataclasses.
Field names are verified against Groww's official API docs
(https://groww.in/trade-api/docs/python-sdk/orders and
.../portfolio) and the installed SDK source, not guessed. A missing
required field still raises ``GrowwTransportError`` rather than silently
defaulting, so an actual future shape change is loud and immediate
instead of producing a corrupted ``Order``/``Position``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.broker.groww_errors import GrowwTransportError


@dataclass(frozen=True)
class GrowwCredentials:
    """
    API credentials for authenticating against the Groww API.

    ``api_key`` + ``secret`` are the two values Groww's dashboard hands
    out together from its "Generate TOTP token" flow - ``secret`` is a
    base32 TOTP seed (the value shown under the TOTP QR code), not a
    static password: a live 6-digit code is computed from it at call
    time (see ``core/utils/totp.py``) and exchanged, along with
    ``api_key``, for a short-lived access token via
    ``GrowwAPI.get_access_token()`` - see ``groww_transport.py``. This
    was confirmed against the installed growwapi SDK's own source after
    the plain-secret assumption failed against a real account (SDK
    accepts either a live ``totp`` or a static ``secret``, and Groww's
    TOTP-flow dashboard only ever gives you the former).
    """

    api_key: str
    secret: str


@dataclass(frozen=True)
class GrowwOrderRequest:
    """
    Outbound order payload shape, built from an internal ``OrderRequest``.

    ``validity`` has no equivalent in this platform's broker-independent
    ``OrderRequest`` - ``GrowwBrokerAdapter`` fills it in with a fixed V1
    default (``VALIDITY_DAY``) when building this model, since the
    platform doesn't expose it as a per-order concept yet. ``segment`` is
    not modeled per-order at all: ``GrowwHttpTransport`` fixes every call
    to the cash segment (see that module's ``_SEGMENT`` constant), since
    this platform is cash-equities-only in V1.

    ``order_reference_id`` carries ``OrderRequest.client_order_id``
    through verbatim (see ``core/execution/idempotency.py``) - Groww's
    own real request field for the same purpose. Left ``None`` when the
    caller didn't set one, in which case Groww's SDK generates its own
    random reference id.
    """

    exchange: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    product_type: str
    validity: str
    price: float | None = None
    trigger_price: float | None = None
    order_reference_id: str | None = None


@dataclass(frozen=True)
class GrowwOrderResponse:
    """
    Order state as returned by Groww, for a placement/cancel response or a
    full order-detail fetch.

    ``symbol``/``exchange``/``side``/``order_type``/``product_type``/
    ``quantity`` are optional: Groww's verified place-order response only
    carries ``groww_order_id``/``order_status``/``order_reference_id``/
    ``remark`` (the adapter already knows the rest from the
    ``OrderRequest`` it just sent) - only the full order-detail fetch
    (``get_order_detail``, used by ``get_order()``) returns them, since
    there is no original request to fall back on there.
    """

    order_id: str
    status: str
    order_reference_id: str | None = None
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
    def from_place_order_json(cls, data: dict[str, Any]) -> "GrowwOrderResponse":
        """
        Parse a place-order response.

        Verified shape (https://groww.in/trade-api/docs/python-sdk/orders):
        ``{"groww_order_id": str, "order_status": str,
        "order_reference_id": str, "remark": str}``. ``average_price``/
        ``filled_quantity`` are not part of the documented shape (a
        freshly-placed order often has no fill yet) but are read
        defensively in case Groww returns them for an instant fill.
        """
        try:
            return cls(
                order_id=data["groww_order_id"],
                status=data["order_status"],
                order_reference_id=data.get("order_reference_id"),
                average_price=data.get("average_price"),
                filled_quantity=int(data.get("filled_quantity", 0)),
                raw_response=data,
            )
        except KeyError as exc:
            raise GrowwTransportError(
                f"Malformed Groww place-order response, missing field: {exc}"
            ) from exc

    @classmethod
    def from_order_detail_json(cls, data: dict[str, Any]) -> "GrowwOrderResponse":
        """
        Parse a ``get_order_detail`` response.

        Unlike a placement response, a fetched order carries no original
        ``OrderRequest`` to fall back on, so the full order shape is
        required here. Verified shape (same docs page): adds
        ``trading_symbol``, ``exchange``, ``transaction_type``,
        ``order_type``, ``product``, ``quantity``, ``average_fill_price``
        to the placement fields above.
        """
        try:
            return cls(
                order_id=data["groww_order_id"],
                status=data["order_status"],
                order_reference_id=data.get("order_reference_id"),
                average_price=data.get("average_fill_price"),
                filled_quantity=int(data.get("filled_quantity", 0)),
                symbol=data["trading_symbol"],
                exchange=data["exchange"],
                side=data["transaction_type"],
                order_type=data["order_type"],
                product_type=data["product"],
                quantity=int(data["quantity"]),
                raw_response=data,
            )
        except KeyError as exc:
            raise GrowwTransportError(
                f"Malformed Groww order-detail response, missing field: {exc}"
            ) from exc


@dataclass(frozen=True)
class GrowwCancelResponse:
    """Outcome of a cancel request, as returned by Groww."""

    order_id: str
    status: str
    raw_response: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "GrowwCancelResponse":
        """
        Parse a cancel-order response.

        Verified shape (https://groww.in/trade-api/docs/python-sdk/orders):
        ``{"groww_order_id": str, "order_status": str}``.
        """
        try:
            return cls(order_id=data["groww_order_id"], status=data["order_status"], raw_response=data)
        except KeyError as exc:
            raise GrowwTransportError(
                f"Malformed Groww cancel-order response, missing field: {exc}"
            ) from exc


@dataclass(frozen=True)
class GrowwPosition:
    """
    A single open position as returned by Groww.

    ``product_type`` still carries Groww's raw product code (``"MIS"``/
    ``"CNC"``) here, not this platform's ``ProductType`` vocabulary -
    ``GrowwBrokerAdapter._map_position`` does that translation, this
    model just captures the wire shape as-is.
    """

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
    def from_json(cls, data: dict[str, Any]) -> "GrowwPosition":
        """
        Parse a single position entry from ``get_positions_for_user``'s
        response.

        Verified shape (https://groww.in/trade-api/docs/python-sdk/portfolio):
        ``{"trading_symbol", "exchange", "quantity", "net_price",
        "product", "realised_pnl", ...}`` (plus credit/debit/carry-forward
        detail this platform doesn't use). Groww's positions response has
        no live last_price/unrealized_pnl - those stay ``None`` here; a
        caller wanting live valuation would need a separate quote call,
        which this adapter does not make automatically.
        """
        try:
            return cls(
                symbol=data["trading_symbol"],
                exchange=data["exchange"],
                quantity=int(data["quantity"]),
                average_price=float(data["net_price"]),
                product_type=data["product"],
                realized_pnl=data.get("realised_pnl"),
                raw_response=data,
            )
        except KeyError as exc:
            raise GrowwTransportError(
                f"Malformed Groww position payload, missing field: {exc}"
            ) from exc

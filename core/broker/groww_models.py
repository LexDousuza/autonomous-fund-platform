"""
Groww-specific wire-shape models.

These dataclasses mirror what the Groww API sends/receives and never leak
outside ``core/broker/groww_adapter.py``. Application code only ever sees
the broker-independent models in ``core/broker/base.py`` (``OrderRequest``,
``Order``, ``Position``); ``GrowwBrokerAdapter`` is the sole translator
between the two.

The ``from_*_json`` classmethods below parse the raw ``dict`` payloads
returned by ``GrowwHttpTransport`` into these dataclasses. Payload shapes
are assumed (not verified against real Groww API docs - see
``groww_transport.py``'s module docstring); a missing required field
raises ``GrowwTransportError`` rather than silently defaulting, so a
shape mismatch is loud and immediate instead of producing a corrupted
``Order``/``Position``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.broker.groww_errors import GrowwTransportError


@dataclass(frozen=True)
class GrowwCredentials:
    """API credentials for authenticating against the Groww API."""

    api_key: str
    access_token: str


@dataclass(frozen=True)
class GrowwOrderRequest:
    """Outbound order payload shape, built from an internal ``OrderRequest``."""

    exchange: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    product_type: str
    price: float | None = None
    trigger_price: float | None = None


@dataclass(frozen=True)
class GrowwOrderResponse:
    """
    Order state as returned by Groww, for both a placement response and a
    full order fetch.

    ``symbol``/``exchange``/``side``/``order_type``/``product_type``/
    ``quantity`` are optional because a placement response only needs to
    carry the order-id/status/fill fields (the adapter already knows the
    rest from the ``OrderRequest`` it just sent); a fetched order
    (``get_order``) always populates them, since there is no original
    request to fall back on.
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
    def from_place_order_json(cls, data: dict[str, Any]) -> "GrowwOrderResponse":
        """
        Parse a place-order transport response.

        Assumed shape: ``{"order_id": str, "status": str,
        "average_price": float | None, "filled_quantity": int}``.
        """
        try:
            return cls(
                order_id=data["order_id"],
                status=data["status"],
                average_price=data.get("average_price"),
                filled_quantity=int(data.get("filled_quantity", 0)),
                raw_response=data,
            )
        except KeyError as exc:
            raise GrowwTransportError(
                f"Malformed Groww place-order response, missing field: {exc}"
            ) from exc

    @classmethod
    def from_get_order_json(cls, data: dict[str, Any]) -> "GrowwOrderResponse":
        """
        Parse a get-order transport response.

        Unlike a placement response, a fetched order carries no original
        ``OrderRequest`` to fall back on, so the full order shape is
        required here. Assumed shape adds ``symbol``, ``exchange``,
        ``side``, ``order_type``, ``product_type``, ``quantity`` to the
        placement fields above.
        """
        try:
            return cls(
                order_id=data["order_id"],
                status=data["status"],
                average_price=data.get("average_price"),
                filled_quantity=int(data.get("filled_quantity", 0)),
                symbol=data["symbol"],
                exchange=data["exchange"],
                side=data["side"],
                order_type=data["order_type"],
                product_type=data["product_type"],
                quantity=int(data["quantity"]),
                raw_response=data,
            )
        except KeyError as exc:
            raise GrowwTransportError(
                f"Malformed Groww get-order response, missing field: {exc}"
            ) from exc


@dataclass(frozen=True)
class GrowwCancelResponse:
    """Outcome of a cancel request, as returned by Groww."""

    order_id: str
    status: str
    raw_response: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "GrowwCancelResponse":
        """Parse a cancel-order transport response. Assumed shape: ``{"order_id": str, "status": str}``."""
        try:
            return cls(order_id=data["order_id"], status=data["status"], raw_response=data)
        except KeyError as exc:
            raise GrowwTransportError(
                f"Malformed Groww cancel-order response, missing field: {exc}"
            ) from exc


@dataclass(frozen=True)
class GrowwPosition:
    """
    A single open position as returned by Groww.

    ``product_type`` is assumed to already be normalized to this
    platform's ``ProductType`` vocabulary (``"INTRADAY"``/``"DELIVERY"``);
    translating Groww's own raw product codes (e.g. ``"MIS"``, ``"CNC"``)
    into that vocabulary is assumed to already have happened by the time
    this model is built, not something this model does itself.
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
        Parse a single position entry from a list-positions transport
        response. Assumed shape: ``{"symbol": str, "exchange": str,
        "quantity": int, "average_price": float, "product_type": str,
        "last_price"/"unrealized_pnl"/"realized_pnl": float | None}``.
        """
        try:
            return cls(
                symbol=data["symbol"],
                exchange=data["exchange"],
                quantity=int(data["quantity"]),
                average_price=float(data["average_price"]),
                product_type=data["product_type"],
                last_price=data.get("last_price"),
                unrealized_pnl=data.get("unrealized_pnl"),
                realized_pnl=data.get("realized_pnl"),
                raw_response=data,
            )
        except KeyError as exc:
            raise GrowwTransportError(
                f"Malformed Groww position payload, missing field: {exc}"
            ) from exc

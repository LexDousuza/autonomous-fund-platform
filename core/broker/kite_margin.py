"""
Kite margin calculator - wraps ``KiteHttpTransport.get_order_margins`` so
``core.risk.options_margin.OptionsMarginRiskManager`` never has to know
Kite's specific request/response shape. SPAN margin math itself is
never reimplemented here (see ``core.broker.margin.MarginCalculator``'s
docstring) - this only translates to/from Kite's real API.
"""

from __future__ import annotations

from core.broker.base import OrderRequest
from core.broker.kite_errors import KiteTransportError
from core.broker.kite_transport import KiteHttpTransport
from core.broker.margin import MarginCalculator

_VARIETY_REGULAR = "regular"
_ORDER_TYPE_TO_KITE = {"MARKET": "MARKET", "LIMIT": "LIMIT", "STOP": "SL"}
_PRODUCT_TYPE_TO_KITE = {"INTRADAY": "MIS", "DELIVERY": "CNC"}


class KiteMarginCalculator(MarginCalculator):
    """
    Calls Kite's real order-margin-calculator endpoint for a single
    order. Verified response shape
    (https://kite.trade/docs/connect/v3/margins/): each entry carries a
    ``total`` field - the total margin block required for that order -
    not yet exercised against a real account, matching this platform's
    existing honesty about untested edge cases elsewhere in the Kite
    integration.
    """

    def __init__(self, transport: KiteHttpTransport) -> None:
        self._transport = transport

    def required_margin(self, order_request: OrderRequest) -> float:
        order_payload = {
            "exchange": order_request.exchange,
            "tradingsymbol": order_request.symbol,
            "transaction_type": order_request.side.value,
            "variety": _VARIETY_REGULAR,
            "product": _PRODUCT_TYPE_TO_KITE[order_request.product_type.value],
            "order_type": _ORDER_TYPE_TO_KITE[order_request.order_type.value],
            "quantity": order_request.quantity,
            "price": order_request.price,
            "trigger_price": order_request.trigger_price,
        }
        response = self._transport.get_order_margins([order_payload])
        if not response:
            raise KiteTransportError("Kite returned no margin entries for the requested order")
        try:
            return float(response[0]["total"])
        except KeyError as exc:
            raise KiteTransportError(
                f"Malformed Kite order-margin response, missing field: {exc}"
            ) from exc


def fetch_available_margin(transport: KiteHttpTransport, segment: str = "equity") -> float:
    """
    Fetch the account's current available margin for ``segment``
    (default ``"equity"``, which also covers NFO equity F&O - Kite has no
    separate options segment). Meant to be called once per live session
    and threaded into ``TradingSessionConfig.available_margin`` (see
    ``apps/live_trader/main.py``) - never per-order, unlike
    ``KiteMarginCalculator.required_margin`` above.
    """
    response = transport.get_margins(segment=segment)
    try:
        return float(response["net"])
    except KeyError as exc:
        raise KiteTransportError(
            f"Malformed Kite margins response, missing field: {exc}"
        ) from exc

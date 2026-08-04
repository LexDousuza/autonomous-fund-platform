"""
Groww margin calculator - wraps ``GrowwHttpTransport.get_order_margin_details``
so ``core.risk.options_margin.OptionsMarginRiskManager`` never has to
know Groww's specific request/response shape. SPAN margin math itself is
never reimplemented here (see ``core.broker.margin.MarginCalculator``'s
docstring) - this only translates to/from Groww's real API.
"""

from __future__ import annotations

from core.broker.base import OrderRequest
from core.broker.groww_errors import GrowwTransportError
from core.broker.groww_transport import GrowwHttpTransport
from core.broker.margin import MarginCalculator

_SEGMENT_FNO = "FNO"

# Our OrderType/ProductType <-> Groww's raw codes, mirroring
# groww_adapter.py's own mapping tables (kept local rather than
# imported, since importing a private module-level dict across files
# would couple this module to groww_adapter.py's internals).
_ORDER_TYPE_TO_GROWW = {"MARKET": "MARKET", "LIMIT": "LIMIT", "STOP": "SL"}
_PRODUCT_TYPE_TO_GROWW = {"INTRADAY": "MIS", "DELIVERY": "CNC"}


class GrowwMarginCalculator(MarginCalculator):
    """
    Calls Groww's real order-margin-calculator endpoint for a single
    order. Verified response shape
    (https://groww.in/trade-api/docs/python-sdk/margin): a
    ``total_requirement`` field carries the total margin needed for the
    (single-order) batch sent - not yet exercised against a real
    account, matching this platform's existing honesty about untested
    edge cases elsewhere in the Groww integration.
    """

    def __init__(self, transport: GrowwHttpTransport) -> None:
        self._transport = transport

    def required_margin(self, order_request: OrderRequest) -> float:
        order_payload = {
            "trading_symbol": order_request.symbol,
            "transaction_type": order_request.side.value,
            "quantity": order_request.quantity,
            "price": order_request.price,
            "order_type": _ORDER_TYPE_TO_GROWW[order_request.order_type.value],
            "product": _PRODUCT_TYPE_TO_GROWW[order_request.product_type.value],
            "exchange": order_request.exchange,
        }
        response = self._transport.get_order_margin_details([order_payload], segment=_SEGMENT_FNO)
        try:
            return float(response["total_requirement"])
        except KeyError as exc:
            raise GrowwTransportError(
                f"Malformed Groww order-margin response, missing field: {exc}"
            ) from exc

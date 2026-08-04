"""
Simulated margin calculator for paper trading only - never used for a
real live session (those use ``KiteMarginCalculator``/
``GrowwMarginCalculator``, which call a broker's real margin endpoint).

Paper trading has no real margin concept at all (see ``PaperBroker``'s
own limitations), so without this, a naked-write-open in paper is
approved purely on ordinary capital-sizing grounds - the opposite of
what a responsible paper-test needs, since the point of paper-testing a
margin-gated strategy is to exercise that gate before it's ever hit for
real.
"""

from __future__ import annotations

from core.broker.base import OrderRequest
from core.broker.margin import MarginCalculator

# ponytail: a naive fixed-percentage-of-notional heuristic, not real
# SPAN margin math - fine here because this calculator's only job is to
# exercise OptionsMarginRiskManager's fail-closed code path in paper
# sessions, not to model real capital requirements. Upgrade path: if
# paper-test P&L ever needs to reflect realistic capital efficiency, a
# real SPAN estimation library (not this class) would be the fix.
_ESTIMATED_MARGIN_FRACTION = 0.20


class EstimatedSpanMarginCalculator(MarginCalculator):
    """
    Estimates required margin as a flat fraction of notional, where
    notional is derived from ``strike`` (a proxy for the underlying's
    price) rather than the option's own premium - using premium as the
    base would understate real SPAN margin by orders of magnitude
    (premium is what a *buyer* pays; margin is what a *seller* must post
    against the underlying's full notional), which is the wrong
    direction for a deliberately conservative simulation.
    """

    def __init__(self, margin_fraction: float = _ESTIMATED_MARGIN_FRACTION) -> None:
        self._margin_fraction = margin_fraction

    def required_margin(self, order_request: OrderRequest) -> float:
        notional = (order_request.strike or 0.0) * order_request.quantity
        return notional * self._margin_fraction

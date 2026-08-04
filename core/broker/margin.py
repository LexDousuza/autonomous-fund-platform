"""
Broker margin-requirement calculators.

A naked option write's true risk isn't the premium received, it's
whatever SPAN margin the exchange/broker demands to hold the position -
computing that correctly is a small research project on its own (see
Groww's ``get_order_margin_details`` / Kite's ``order_margins``), so it
is never reimplemented here. ``MarginCalculator`` is the seam
``core.risk.options_margin.OptionsMarginRiskManager`` calls into; each
broker gets its own thin implementation (``core/broker/groww_margin.py``/
``core/broker/kite_margin.py``) wrapping that broker's own margin
endpoint.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.broker.base import OrderRequest


class MarginCalculator(ABC):
    """Returns the margin (in rupees) a broker would require to open ``order_request``."""

    @abstractmethod
    def required_margin(self, order_request: OrderRequest) -> float:
        raise NotImplementedError

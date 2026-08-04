from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class ProductType(str, Enum):
    INTRADAY = "INTRADAY"
    DELIVERY = "DELIVERY"


class OptionType(str, Enum):
    """
    NSE options have been European-style (no early exercise) since
    SEBI's 2019 circular - ``core/pricing/black_scholes.py`` relies on
    that. Lives here, not in ``core/pricing``, since it's shared trading
    vocabulary every layer (broker, strategy, pricing) imports from.
    """

    CALL = "CALL"
    PUT = "PUT"


class PositionEffect(str, Enum):
    """
    Required (not optional) on every options order/signal: a SELL is
    ambiguous for an option in a way it never is for a cash-equity share
    - closing a long call (risk-reducing) and opening a naked short call
    (risk-creating, uncapped) are both "SELL", and nothing else in the
    order model disambiguates them. Not meaningful for cash-equity
    orders, which stay ``None``.
    """

    OPEN = "OPEN"
    CLOSE = "CLOSE"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"


@dataclass(slots=True)
class OrderRequest:
    """
    ``client_order_id`` is an optional caller-supplied idempotency key
    for this specific order intent (see ``core/execution/idempotency.py``).
    Callers that don't set it get today's behavior unchanged -
    ``PaperBroker`` always creates a new fill, and ``GrowwBrokerAdapter``
    lets Groww generate its own reference id.

    ``algo_id`` is the broker/exchange-issued algo-strategy identifier
    SEBI's retail algo-trading framework requires on every order an
    algorithm places (distinct from ``client_order_id``, which is this
    platform's own internal idempotency key, not a regulatory tag) - see
    ``apps/live_trader/config.py``'s ``LIVE_BROKER_ALGO_ID``. ``None`` for
    a paper session, which places no real exchange order. When set,
    ``KiteBrokerAdapter``/``GrowwBrokerAdapter`` prefer it over
    ``client_order_id`` for whichever single wire field their broker
    reuses for both concerns (Kite's ``tag``, Groww's
    ``order_reference_id``) - the compliance tag wins when both would
    otherwise compete for one field.

    ``strike``/``expiry``/``option_type``/``lot_size``/``position_effect``
    are all ``None``/``1`` for a plain cash-equity order (today's only
    kind) - every existing call site is byte-identical. For an options
    order, ``option_type`` set is the signal that the others apply;
    ``position_effect`` is then required in practice (see
    ``PositionEffect``'s docstring), though not enforced by the type
    system to avoid a runtime check every cash-equity call site would
    have to satisfy for a field that doesn't apply to it.
    """

    symbol: str
    exchange: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    product_type: ProductType
    price: float | None = None
    trigger_price: float | None = None
    strategy_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    client_order_id: str | None = None
    algo_id: str | None = None
    strike: float | None = None
    expiry: str | None = None
    option_type: OptionType | None = None
    lot_size: int = 1
    position_effect: PositionEffect | None = None


@dataclass(slots=True)
class Order:
    order_id: str
    symbol: str
    exchange: str
    side: OrderSide
    quantity: int
    filled_quantity: int
    status: OrderStatus
    order_type: OrderType
    product_type: ProductType
    created_at: datetime | None = None
    updated_at: datetime | None = None
    average_price: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    strike: float | None = None
    expiry: str | None = None
    option_type: OptionType | None = None
    lot_size: int = 1
    position_effect: PositionEffect | None = None


@dataclass(slots=True)
class Position:
    """
    ``quantity`` is signed for an options position: positive is long,
    negative is a naked short (see ``PositionEffect``) - mirrors how
    ``core/execution/kill_switch.py``'s ``_build_flatten_request``
    already reads this field for any product type.
    """

    symbol: str
    exchange: str
    quantity: int
    average_price: float
    product_type: ProductType
    last_price: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    strike: float | None = None
    expiry: str | None = None
    option_type: OptionType | None = None
    lot_size: int = 1


class Broker(ABC):
    """
    Abstract broker interface. Concrete broker implementations (e.g. Groww)
    must implement these methods.
    """

    @abstractmethod
    def place_order(self, order_request: OrderRequest) -> Order:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_order(self, order_id: str) -> Order:
        raise NotImplementedError

    @abstractmethod
    def list_positions(self) -> list[Position]:
        raise NotImplementedError
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


@dataclass(slots=True)
class Position:
    symbol: str
    exchange: str
    quantity: int
    average_price: float
    product_type: ProductType
    last_price: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None


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
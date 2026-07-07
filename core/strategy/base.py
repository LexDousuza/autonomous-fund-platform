from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SignalSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    EXIT = "EXIT"


@dataclass(slots=True)
class MarketBar:
    symbol: str
    exchange: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(slots=True)
class StrategyContext:
    strategy_name: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TradeSignal:
    strategy_name: str
    symbol: str
    exchange: str
    side: SignalSide
    timestamp: datetime
    reason: str
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    """
    Base interface for all trading strategies.

    A strategy consumes market data and current state, then optionally
    emits one or more trade signals.
    """

    def __init__(self, context: StrategyContext) -> None:
        self.context = context

    @abstractmethod
    def on_bar(
        self,
        bar: MarketBar,
        history: list[MarketBar],
        open_positions: dict[str, Any],
    ) -> list[TradeSignal]:
        """
        Process one incoming bar and return zero or more trade signals.
        """
        raise NotImplementedError
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from core.broker.base import OptionType, PositionEffect


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
    """
    ``position_effect`` is unset for a plain cash-equity signal (today's
    only kind - every existing strategy is byte-identical). For an
    options signal, it's required in practice: a SELL is ambiguous
    between closing a long and opening a naked short (see
    ``core.broker.base.PositionEffect``'s docstring) - the strategy is
    the one place that knows which it means, so it must say so here for
    ``core.execution.orchestrator.build_order_from_signal`` to carry it
    through unambiguously.

    ``requires_prior_fill``: for a multi-leg strategy returning more than
    one signal from a single ``on_bar()`` call (e.g. a credit spread's
    protective long leg followed by its short leg), set on every signal
    after the first in the chain. ``core.execution.orchestrator.
    ExecutionOrchestrator.run_session`` only attempts such a signal if the
    immediately preceding signal in that same returned list actually
    filled, and sizes it at that fill's exact quantity rather than
    independently - see the orchestrator's own comment at its signal loop
    for why (legs priced differently would otherwise size to mismatched
    quantities, silently breaking the intended hedge). ``False`` (the
    default) is byte-identical for every existing single-signal strategy.
    """

    strategy_name: str
    symbol: str
    exchange: str
    side: SignalSide
    timestamp: datetime
    reason: str
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    strike: float | None = None
    expiry: str | None = None
    option_type: OptionType | None = None
    lot_size: int = 1
    position_effect: PositionEffect | None = None
    requires_prior_fill: bool = False


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
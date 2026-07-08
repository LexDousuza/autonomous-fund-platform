from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.portfolio.exit_models import ExitDecision, ExitPolicyConfig, PositionSnapshot
from core.portfolio.exits import ExitPolicyEvaluator
from core.strategy.base import MarketBar


@dataclass(slots=True)
class TrackedPosition:
    """
    Lifecycle bookkeeping for one open position.

    Kept separate from the broker's ``Position`` because it records
    information (entry time, bars held) a broker does not need to track.
    """

    symbol: str
    exchange: str
    quantity: int
    entry_price: float
    entry_time: datetime
    strategy_name: str
    bars_held: int = 0


class PositionManager:
    """
    Tracks the lifecycle of positions opened during a trading session and
    evaluates exit rules against incoming bars.

    Broker-independent: it only records what it is told (entry fills) and
    reports exit decisions; it never places orders or talks to a broker
    directly. That is the orchestrator's responsibility.
    """

    def __init__(self, evaluator: ExitPolicyEvaluator | None = None) -> None:
        self._positions: dict[str, TrackedPosition] = {}
        self._evaluator = evaluator or ExitPolicyEvaluator()

    @staticmethod
    def _key(exchange: str, symbol: str) -> str:
        return f"{exchange}:{symbol}"

    def register_entry(
        self,
        symbol: str,
        exchange: str,
        quantity: int,
        entry_price: float,
        entry_time: datetime,
        strategy_name: str,
    ) -> None:
        """Start tracking a newly opened position."""
        self._positions[self._key(exchange, symbol)] = TrackedPosition(
            symbol=symbol,
            exchange=exchange,
            quantity=quantity,
            entry_price=entry_price,
            entry_time=entry_time,
            strategy_name=strategy_name,
        )

    def has_open_position(self, symbol: str, exchange: str) -> bool:
        return self._key(exchange, symbol) in self._positions

    def clear(self, symbol: str, exchange: str) -> None:
        """Stop tracking a position, typically once it has been exited."""
        self._positions.pop(self._key(exchange, symbol), None)

    def open_positions(self) -> dict[str, TrackedPosition]:
        """Return a snapshot of currently tracked positions, keyed by ``exchange:symbol``."""
        return dict(self._positions)

    def evaluate_exits(self, bar: MarketBar, policy: ExitPolicyConfig) -> list[ExitDecision]:
        """
        Advance the holding-bar count and evaluate exit rules for the
        tracked position matching ``bar``'s symbol/exchange, if any.

        Returns a list so callers have a uniform shape to iterate over,
        but V1 only ever tracks one position per symbol/exchange, so the
        list contains at most one decision.
        """
        key = self._key(bar.exchange, bar.symbol)
        tracked = self._positions.get(key)
        if tracked is None:
            return []

        tracked.bars_held += 1
        snapshot = PositionSnapshot(
            symbol=tracked.symbol,
            exchange=tracked.exchange,
            quantity=tracked.quantity,
            entry_price=tracked.entry_price,
            entry_time=tracked.entry_time,
            current_price=bar.close,
            current_time=bar.timestamp,
            bars_held=tracked.bars_held,
            strategy_name=tracked.strategy_name,
        )
        decision = self._evaluator.evaluate(snapshot, policy)
        return [decision] if decision.should_exit else []

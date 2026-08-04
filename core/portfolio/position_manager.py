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
    ``best_price_since_entry``/``last_price`` are set to ``entry_price``
    at registration (see ``register_entry``) - never actually ``0.0``
    once tracked, that default only holds before a position is
    registered. ``last_price`` persists the most recent bar close seen
    for this specific position (updated every ``evaluate_exits`` call) -
    unlike ``PositionSnapshot.current_price``, computed fresh per call
    and discarded, this is what lets a caller read another, sibling
    position's latest known price without needing that position's own
    bar to have just streamed (see ``group_id``/``evaluate_group_exits``).

    ``group_id`` is ``None`` for every ordinary single-leg position
    (the default, untouched by any existing strategy). A strategy
    emitting multiple legs from one signal chain (e.g.
    ``core.strategy.credit_spread``'s long+short pair) that need to be
    evaluated and closed together, as one joint decision rather than two
    independent per-leg ones, tags them with the same ``group_id`` - see
    ``evaluate_group_exits``.
    """

    symbol: str
    exchange: str
    quantity: int
    entry_price: float
    entry_time: datetime
    strategy_name: str
    bars_held: int = 0
    best_price_since_entry: float = 0.0
    last_price: float = 0.0
    group_id: str | None = None


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
        group_id: str | None = None,
    ) -> None:
        """Start tracking a newly opened position. ``group_id`` - see ``TrackedPosition``'s docstring."""
        self._positions[self._key(exchange, symbol)] = TrackedPosition(
            symbol=symbol,
            exchange=exchange,
            quantity=quantity,
            entry_price=entry_price,
            entry_time=entry_time,
            strategy_name=strategy_name,
            best_price_since_entry=entry_price,
            last_price=entry_price,
            group_id=group_id,
        )

    def has_open_position(self, symbol: str, exchange: str) -> bool:
        return self._key(exchange, symbol) in self._positions

    def owned_by_other_strategy(
        self, symbol: str, exchange: str, strategy_name: str
    ) -> str | None:
        """
        Return the strategy_name currently holding this symbol if it is
        someone other than ``strategy_name``, else ``None``.

        A multi-strategy session shares one ``PositionManager`` across all
        active strategies (so exit evaluation stays unified); this is the
        exclusivity check that keeps two strategies from both entering the
        same symbol at once. It has to live here, not at the broker layer:
        ``core/broker/base.py``'s ``Position`` has no per-strategy
        attribution (a real brokerage account has no such concept, and
        ``PaperBroker`` blends same-symbol fills from any caller into one
        position) - this in-process check is what actually enforces "at
        most one strategy per symbol," not the broker's position book.
        """
        tracked = self._positions.get(self._key(exchange, symbol))
        if tracked is None or tracked.strategy_name == strategy_name:
            return None
        return tracked.strategy_name

    def clear(self, symbol: str, exchange: str) -> None:
        """Stop tracking a position, typically once it has been exited."""
        self._positions.pop(self._key(exchange, symbol), None)

    def reduce_quantity(self, symbol: str, exchange: str, filled_quantity: int) -> None:
        """
        Reduce a tracked position's quantity toward zero by
        ``filled_quantity`` (an unsigned magnitude - however much of a
        partial exit fill actually executed), preserving entry_price/
        entry_time/bars_held/best_price_since_entry for whatever remains
        open - unlike ``clear()``, which drops the position entirely.

        Moves toward zero regardless of long/short (mirrors
        ``TrackedPosition.quantity``'s own sign convention - positive
        long, negative short) by reading the *current* sign rather than
        requiring the caller to know it. Stops tracking entirely (same
        as ``clear()``) once nothing remains. No-op if the symbol isn't
        tracked at all.
        """
        key = self._key(exchange, symbol)
        tracked = self._positions.get(key)
        if tracked is None:
            return
        direction = 1 if tracked.quantity > 0 else -1
        tracked.quantity -= direction * filled_quantity
        if tracked.quantity == 0:
            self._positions.pop(key, None)

    def open_positions(self) -> dict[str, TrackedPosition]:
        """Return a snapshot of currently tracked positions, keyed by ``exchange:symbol``."""
        return dict(self._positions)

    def evaluate_exits(
        self, bar: MarketBar, policy: ExitPolicyConfig, atr: float | None = None
    ) -> list[ExitDecision]:
        """
        Advance the holding-bar count and evaluate exit rules for the
        tracked position matching ``bar``'s symbol/exchange, if any.

        ``atr`` is the caller's already-computed average true range for
        this symbol (see ``core.analytics.indicators.average_true_range``),
        only consulted when ``policy`` sets an ATR-based stop/target
        multiplier - omit it (default ``None``) for a session that only
        uses flat-percent rules, which behaves exactly as before this
        parameter existed.

        Returns a list so callers have a uniform shape to iterate over,
        but V1 only ever tracks one position per symbol/exchange, so the
        list contains at most one decision.
        """
        key = self._key(bar.exchange, bar.symbol)
        tracked = self._positions.get(key)
        if tracked is None:
            return []

        tracked.bars_held += 1
        tracked.last_price = bar.close
        if tracked.quantity < 0:
            tracked.best_price_since_entry = min(tracked.best_price_since_entry, bar.close)
        else:
            tracked.best_price_since_entry = max(tracked.best_price_since_entry, bar.close)
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
            atr=atr,
            best_price_since_entry=tracked.best_price_since_entry,
        )
        decision = self._evaluator.evaluate(snapshot, policy)
        return [decision] if decision.should_exit else []

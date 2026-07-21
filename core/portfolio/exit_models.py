from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any


def parse_time_of_day(value: str) -> time:
    """
    Parse an ``"HH:MM"`` (24-hour) string - the format
    ``DailyRiskLimits.force_square_off_at``/``stop_new_entries_after`` are
    authored in (see ``config/mandates/sample_mandate.yaml``) - into a
    ``time``. Raises ``ValueError`` for a malformed value, so a typo'd
    mandate fails loudly rather than silently never squaring off.
    """
    return datetime.strptime(value, "%H:%M").time()


@dataclass(slots=True)
class ExitPolicyConfig:
    """
    V1 intraday exit rules for a single trading session.

    ``stop_loss_pct`` and ``target_pct`` are percentage points relative to
    entry price (e.g. ``1.5`` means 1.5%). Any rule left as ``None`` is
    skipped. Assumes long-only positions, matching the platform's current
    long-only strategies.
    """

    stop_loss_pct: float | None = None
    target_pct: float | None = None
    square_off_time: time | None = None
    max_holding_bars: int | None = None


@dataclass(slots=True)
class PositionSnapshot:
    """
    Point-in-time view of an open position, used as input to exit rule
    evaluation. Separate from the broker's ``Position`` because it also
    carries lifecycle information (entry time, bars held) that a broker
    does not track.
    """

    symbol: str
    exchange: str
    quantity: int
    entry_price: float
    entry_time: datetime
    current_price: float
    current_time: datetime
    bars_held: int
    strategy_name: str


@dataclass(slots=True)
class ExitDecision:
    """
    Outcome of evaluating exit rules for one position on one bar.

    ``reason`` identifies which rule triggered the exit (e.g.
    ``"stop_loss"``, ``"target"``, ``"square_off"``, ``"max_holding_bars"``)
    so decisions remain inspectable and auditable.
    """

    should_exit: bool
    symbol: str
    exchange: str
    quantity: int
    reason: str | None = None
    exit_price: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

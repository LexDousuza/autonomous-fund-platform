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
    skipped. Handles both long and short positions (see
    ``core.portfolio.exits.ExitPolicyEvaluator``'s docstring) - a
    short's stop/target are the mirror image of a long's.

    ``stop_loss_atr_multiplier``/``target_atr_multiplier`` are an
    optional per-symbol-volatility-scaled alternative to the flat
    ``*_pct`` fields (e.g. a 1.5x-ATR stop instead of a flat 1.5% one, so
    a high-beta stock and a blue-chip aren't held to the same fixed move)
    - independent knobs, same as ``stop_loss_pct``/``target_pct``, so a
    session can mix a flat-pct stop with an ATR-based target or vice
    versa. Takes priority over the matching ``*_pct`` field whenever
    ``PositionSnapshot.atr`` is available (i.e. the caller actually
    computed one - see ``core.execution.orchestrator``); falls back to
    ``*_pct`` otherwise, so a session that never supplies an ATR value
    behaves exactly as before this field existed. ``atr_lookback_bars``
    is the window callers should use for that computation (see
    ``core.analytics.indicators.average_true_range``).

    ``trailing_stop_pct`` is an alternative to (or combined with)
    ``target_pct``: instead of exiting at one fixed profit level, ratchet
    the exit trigger as the position moves favorably, off
    ``PositionSnapshot.best_price_since_entry`` (initialized to entry
    price, so this reduces to an ordinary stop before any favorable move,
    and only starts "trailing" once one happens) - letting a winner run
    further while still locking in most of a move once it reverses,
    rather than capping it at one fixed target. ``None`` (the default)
    disables it entirely - existing sessions that never set this see no
    behavior change.
    """

    stop_loss_pct: float | None = None
    target_pct: float | None = None
    square_off_time: time | None = None
    max_holding_bars: int | None = None
    stop_loss_atr_multiplier: float | None = None
    target_atr_multiplier: float | None = None
    atr_lookback_bars: int = 14
    trailing_stop_pct: float | None = None


@dataclass(slots=True)
class PositionSnapshot:
    """
    Point-in-time view of an open position, used as input to exit rule
    evaluation. Separate from the broker's ``Position`` because it also
    carries lifecycle information (entry time, bars held) that a broker
    does not track.

    ``best_price_since_entry`` is the highest close seen since entry for a
    long (``quantity > 0``) or the lowest for a short - only meaningful
    when ``ExitPolicyConfig.trailing_stop_pct`` is set; ``None`` otherwise
    (a caller not using a trailing stop never needs to track it).
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
    atr: float | None = None
    best_price_since_entry: float | None = None


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

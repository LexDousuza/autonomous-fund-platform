from __future__ import annotations

from datetime import date

_DAYS_PER_YEAR = 365.0


def time_to_expiry_years(expiry: str, as_of: date) -> float:
    """
    Years remaining until ``expiry`` (an ISO date string), measured from
    ``as_of`` - never the wall clock. This used to be three separate,
    identical copies (``core/strategy/credit_spread.py``,
    ``core/strategy/options_mispricing.py``, ``core/pricing/iv_resolver.py``),
    each computed from ``date.today()``. That made both options strategies
    structurally incapable of ever producing a signal in a historical
    backtest: replaying past bars against the real wall-clock date always
    yields a negative ``days_remaining``, clamped to 0, so every call
    aborted on every bar. Callers driven by a strategy's ``on_bar`` must
    pass ``bar.timestamp.date()``; callers running at genuine real-time
    session-build time (e.g. live/paper bucket selection) pass
    ``date.today()`` explicitly instead of it being hidden inside this
    function.
    """
    days_remaining = (date.fromisoformat(expiry) - as_of).days
    return max(days_remaining, 0) / _DAYS_PER_YEAR

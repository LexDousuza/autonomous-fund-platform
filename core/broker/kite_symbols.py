"""
Kite/NSE option tradingsymbol construction.

``build_kite_option_tradingsymbol`` is a pure, deterministic formatter
for the standard NSE tradingsymbol convention Zerodha's Kite Connect
uses - useful for building a lookup key or for display. It is
*not* the authoritative source for what gets sent to the broker: an
NSE-assigned tradingsymbol must always be verified against the broker's
own instrument master before an order is placed (see
``core/market_data/kite_option_chain.py``, which resolves the real
tradingsymbol from a live ``instruments("NFO")`` fetch matching
underlying+strike+expiry+option_type) - lot sizes and exact symbols roll
weekly/monthly and are never hardcoded, matching this plan's decision
that they're "always read fresh from the broker's instrument master."

ponytail: this formula is the well-documented public NSE/Kite
convention, not verified against a live instrument dump in this
session (no account/network access here) - treat it as a best-effort
constructor for tests/display, and always cross-check the first real
symbol it produces against ``instruments("NFO")`` before relying on it
for a live order.
"""

from __future__ import annotations

from datetime import date, timedelta

from core.broker.base import OptionType

_MONTH_ABBREVIATIONS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

# Weekly-expiry tradingsymbols (index options like NIFTY/BANKNIFTY that
# expire on a non-last-Thursday weekly cycle) use a single-character
# month code instead of the 3-letter monthly abbreviation: 1-9 for
# Jan-Sep, then O/N/D for Oct/Nov/Dec.
_WEEKLY_MONTH_CODES = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "O", "N", "D")


def _is_last_thursday_of_month(expiry: date) -> bool:
    """True if ``expiry`` is the last Thursday of its month - the monthly-contract expiry day."""
    if expiry.weekday() != 3:  # Thursday
        return False
    days_in_month = (expiry.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return expiry.day + 7 > days_in_month.day


def build_kite_option_tradingsymbol(
    underlying_symbol: str, expiry: date, strike: float, option_type: OptionType
) -> str:
    """
    Build the standard NSE/Kite options tradingsymbol for
    ``underlying_symbol``/``expiry``/``strike``/``option_type``.

    Monthly-expiry format (stock options, and index options on their
    last-Thursday-of-month expiry): ``{SYMBOL}{YY}{MMM}{STRIKE}{CE|PE}``
    e.g. ``RELIANCE26AUG2600CE``.

    Weekly-expiry format (index options on a non-monthly expiry):
    ``{SYMBOL}{YY}{M}{DD}{STRIKE}{CE|PE}`` where ``M`` is a single
    month-code character (``1``-``9``, then ``O``/``N``/``D``) e.g.
    ``NIFTY26O2823000CE`` for an Oct 28 2026 weekly expiry.

    ``strike`` is formatted as a whole number when it has no fractional
    part (the common case for index/large-cap strikes) and otherwise
    with its exact decimal digits, since NSE strikes are occasionally
    non-integer (e.g. some stock options).
    """
    year_suffix = f"{expiry.year % 100:02d}"
    strike_str = str(int(strike)) if strike == int(strike) else f"{strike:g}"

    if _is_last_thursday_of_month(expiry):
        month_part = _MONTH_ABBREVIATIONS[expiry.month - 1]
        date_part = f"{year_suffix}{month_part}"
    else:
        month_code = _WEEKLY_MONTH_CODES[expiry.month - 1]
        date_part = f"{year_suffix}{month_code}{expiry.day:02d}"

    return f"{underlying_symbol}{date_part}{strike_str}{option_type.value[:1]}E"

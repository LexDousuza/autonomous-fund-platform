"""
Order idempotency: deterministic client-side order reference ids plus a
same-session duplicate guard, so a strategy/bookkeeping bug (or a bar
that gets processed twice) that fires the "same" trading decision more
than once results in one order placed, not two.

The reference id is also threaded through to the broker as
``OrderRequest.client_order_id``:

- ``PaperBroker`` treats it as a real idempotency key - a repeated
  ``place_order()`` call with the same ``client_order_id`` returns the
  original fill rather than creating a new one (see
  ``core/broker/paper.py``).
- ``GrowwBrokerAdapter`` passes it through as Groww's own
  ``order_reference_id`` field (verified real field, see
  ``core/broker/groww_transport.py``'s module docstring) - if Groww
  enforces server-side deduplication on it, this platform benefits from
  that too, though that specific server-side behavior is not
  independently verified; Groww's docs describe the field only as "the
  reference id to track the order with," not explicitly as a guaranteed
  dedup key.

Scope: this guards against duplication *within one session* - the
in-memory guard has no memory across process restarts. A session that
crashes and restarts has no record of what it placed before crashing, so
cross-process duplicate protection isn't covered here. That would need
looking up existing orders by reference id from the broker before
placing (Groww's real ``get_order_status_by_reference`` is the verified
hook for that) - a natural next step, not built in this pass.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from core.broker.base import OrderSide


def build_order_reference_id(
    strategy_name: str,
    exchange: str,
    symbol: str,
    side: OrderSide,
    timestamp: datetime,
    intent: str,
) -> str:
    """
    Deterministic idempotency key for one trading decision: the same
    ``(strategy_name, exchange, symbol, side, timestamp, intent)`` always
    produces the same id, so re-processing the same bar/decision is
    detectable instead of silently placing a second order.

    ``intent`` distinguishes an entry from an exit (and different exit
    reasons) for the same symbol/bar - e.g. ``"entry"`` vs
    ``"exit:stop_loss"`` - so the two never collide even when triggered
    by the same bar.

    Hashed (not a raw concatenated string) to stay within whatever
    length Groww's real ``order_reference_id`` field allows - not
    verified, so a short, fixed-length id is the safer choice.
    """
    raw = f"{strategy_name}|{exchange}|{symbol}|{side.value}|{timestamp.isoformat()}|{intent}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class OrderIdempotencyGuard:
    """
    Tracks order reference ids placed so far *in this session* and
    reports whether a given id has already been used.

    Deliberately just a set with one method: this is the in-memory,
    same-run half of duplicate-order protection (see module docstring
    for what it does and does not cover), not a persistence layer.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def check_and_record(self, reference_id: str) -> bool:
        """Returns True the first time ``reference_id`` is seen; False on every repeat."""
        if reference_id in self._seen:
            return False
        self._seen.add(reference_id)
        return True

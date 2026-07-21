"""
Derives per-trade P&L and a cumulative P&L series from a session's audit
event log - the reusable analytics behind the reporting dashboard's
performance charts, kept in core so it stays testable and reusable
independent of how it ends up rendered.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class ClosedTrade:
    """One completed entry-to-exit round trip, derived from a matched
    ``order_placed``/``exit_order_placed`` event pair."""

    symbol: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    exit_timestamp: datetime


def compute_closed_trades(events: list[dict]) -> list[ClosedTrade]:
    """
    Match each ``exit_order_placed`` event to the earliest still-open
    ``order_placed`` entry for the same symbol (FIFO per symbol - this
    platform currently allows at most one open entry per symbol per
    session, so FIFO and "the" open entry coincide in practice) and
    return one ``ClosedTrade`` per matched pair, in exit order. Only
    ``FILLED`` orders are considered; an exit with no matching open
    entry is skipped rather than raising, since the dashboard must keep
    rendering from a partial/mid-session event log.
    """
    open_entries: dict[str, list[tuple[float, int]]] = {}
    closed_trades: list[ClosedTrade] = []

    for event in events:
        payload = event.get("payload", {})
        symbol = event.get("symbol")
        if not symbol or payload.get("status") != "FILLED":
            continue

        if event.get("event_type") == "order_placed":
            open_entries.setdefault(symbol, []).append(
                (payload["average_price"], payload["quantity"])
            )
        elif event.get("event_type") == "exit_order_placed":
            pending = open_entries.get(symbol)
            if not pending:
                continue
            entry_price, entry_quantity = pending.pop(0)
            quantity = min(entry_quantity, payload["quantity"])
            closed_trades.append(
                ClosedTrade(
                    symbol=symbol,
                    entry_price=entry_price,
                    exit_price=payload["average_price"],
                    quantity=quantity,
                    pnl=(payload["average_price"] - entry_price) * quantity,
                    exit_timestamp=datetime.fromisoformat(event["timestamp"]),
                )
            )

    return closed_trades


def compute_cumulative_pnl_series(
    closed_trades: list[ClosedTrade],
) -> list[tuple[datetime, float]]:
    """Running realized P&L after each closed trade, in exit order."""
    running_total = 0.0
    series = []
    for trade in closed_trades:
        running_total += trade.pnl
        series.append((trade.exit_timestamp, running_total))
    return series

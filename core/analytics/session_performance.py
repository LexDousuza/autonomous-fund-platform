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


@dataclass(slots=True)
class PortfolioMetrics:
    """
    Aggregate tearsheet stats across a set of closed trades - meant for
    every trade on record (all sessions), not one session's handful, or
    every ratio below is noise. ``None`` fields mean "not computable from
    what's here" (e.g. no losing trades yet), not zero.
    """

    trade_count: int
    total_pnl: float
    win_rate: float | None
    profit_factor: float | None
    avg_win: float | None
    avg_loss: float | None
    sharpe_ratio: float | None
    max_drawdown: float


def compute_portfolio_metrics(trades: list[ClosedTrade]) -> PortfolioMetrics:
    """
    ``trades`` must already be in chronological (exit) order - the same
    assumption ``compute_cumulative_pnl_series`` makes - since both the
    drawdown walk and the equity-curve semantics depend on it.

    ``sharpe_ratio`` is mean/stdev of each trade's *return on capital
    deployed* (``pnl / (entry_price * quantity)``), sample stdev (n-1) -
    a risk-adjusted-return ratio across the trades on record, not a
    textbook annualized Sharpe (trades don't land on a fixed schedule, so
    there's no clean trades-per-year to scale by). Treat it as noise
    below a few dozen trades, same as any statistic on a small sample -
    ``trade_count`` is returned alongside it for exactly that reason.
    """
    trade_count = len(trades)
    if trade_count == 0:
        return PortfolioMetrics(0, 0.0, None, None, None, None, None, 0.0)

    pnls = [t.pnl for t in trades]
    total_pnl = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)

    win_rate = len(wins) / trade_count
    avg_win = gross_profit / len(wins) if wins else None
    avg_loss = -gross_loss / len(losses) if losses else None
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    returns = [t.pnl / (t.entry_price * t.quantity) for t in trades if t.entry_price and t.quantity]
    sharpe_ratio = None
    if len(returns) >= 2:
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
        stdev = variance**0.5
        sharpe_ratio = (mean_return / stdev) if stdev > 0 else None

    running_total = peak = max_drawdown = 0.0
    for trade in trades:
        running_total += trade.pnl
        peak = max(peak, running_total)
        max_drawdown = max(max_drawdown, peak - running_total)

    return PortfolioMetrics(
        trade_count=trade_count,
        total_pnl=total_pnl,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_win=avg_win,
        avg_loss=avg_loss,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
    )

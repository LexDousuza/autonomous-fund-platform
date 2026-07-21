"""
Backtest performance report.

Aggregates multi-day ``ExecutionOrchestrator`` results
(``core/backtest/day_runner.py``) into the summary statistics that
matter for deciding whether a strategy is worth pursuing - win rate,
profit factor, average win/loss, and max drawdown - computed by matching
entries to exits FIFO per symbol per day (``ExecutionSessionResult``
doesn't carry a pre-paired "trade" concept itself). FIFO pairing is
correct under the default ``allow_multiple_entries=False`` strategy
config: at most one open lot per symbol at a time, so a symbol's
placed/exit orders naturally alternate entry, exit, entry, exit, ... in
chronological order.

This is a report on SYSTEM/STRATEGY MECHANICS over whatever price data
it was run against - see ``core/backtest/synthetic_data.py``'s module
docstring for why a report built from synthetic data must never be
represented as a prediction of real-world profitability.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date

from core.backtest.day_runner import BacktestDayResult


@dataclass(slots=True)
class ClosedTrade:
    trading_date: date
    symbol: str
    exchange: str
    quantity: int
    entry_price: float
    exit_price: float
    exit_reason: str | None
    realized_pnl: float


@dataclass(slots=True)
class BacktestPerformanceReport:
    trading_days: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    total_realized_pnl: float
    win_rate: float
    average_pnl_per_trade: float
    average_win: float
    average_loss: float
    profit_factor: float | None
    max_drawdown: float
    duplicate_orders_blocked: int
    exit_reason_counts: dict[str, int] = field(default_factory=dict)
    closed_trades: list[ClosedTrade] = field(default_factory=list)


def build_performance_report(day_results: list[BacktestDayResult]) -> BacktestPerformanceReport:
    """Build a ``BacktestPerformanceReport`` from a chronological list of per-day results."""
    closed_trades: list[ClosedTrade] = []
    duplicate_orders_blocked = 0

    for day_result in day_results:
        result = day_result.session_result
        duplicate_orders_blocked += result.duplicate_orders_blocked

        open_entries: dict[str, deque] = defaultdict(deque)
        for order in result.placed_orders:
            open_entries[f"{order.exchange}:{order.symbol}"].append(order)

        for exit_decision, exit_order in zip(result.exit_decisions, result.exit_orders):
            queue = open_entries[f"{exit_decision.exchange}:{exit_decision.symbol}"]
            if not queue:
                continue  # no matching entry recorded - shouldn't happen; skip defensively
            entry_order = queue.popleft()
            if entry_order.average_price is None or exit_order.average_price is None:
                continue

            closed_trades.append(
                ClosedTrade(
                    trading_date=day_result.trading_date,
                    symbol=exit_decision.symbol,
                    exchange=exit_decision.exchange,
                    quantity=exit_decision.quantity,
                    entry_price=entry_order.average_price,
                    exit_price=exit_order.average_price,
                    exit_reason=exit_decision.reason,
                    realized_pnl=(exit_order.average_price - entry_order.average_price)
                    * exit_decision.quantity,
                )
            )

    winning = [t for t in closed_trades if t.realized_pnl > 0]
    losing = [t for t in closed_trades if t.realized_pnl < 0]
    breakeven = [t for t in closed_trades if t.realized_pnl == 0]

    total_pnl = sum(t.realized_pnl for t in closed_trades)
    gross_profit = sum(t.realized_pnl for t in winning)
    gross_loss = abs(sum(t.realized_pnl for t in losing))

    exit_reason_counts: dict[str, int] = {}
    for trade in closed_trades:
        if trade.exit_reason:
            exit_reason_counts[trade.exit_reason] = exit_reason_counts.get(trade.exit_reason, 0) + 1

    # Max drawdown over the cumulative-pnl equity curve, in the order
    # trades closed (day order, then within-day exit order).
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in closed_trades:
        cumulative += trade.realized_pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    return BacktestPerformanceReport(
        trading_days=len(day_results),
        total_trades=len(closed_trades),
        winning_trades=len(winning),
        losing_trades=len(losing),
        breakeven_trades=len(breakeven),
        total_realized_pnl=total_pnl,
        win_rate=(len(winning) / len(closed_trades)) if closed_trades else 0.0,
        average_pnl_per_trade=(total_pnl / len(closed_trades)) if closed_trades else 0.0,
        average_win=(gross_profit / len(winning)) if winning else 0.0,
        average_loss=(-gross_loss / len(losing)) if losing else 0.0,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        max_drawdown=max_drawdown,
        duplicate_orders_blocked=duplicate_orders_blocked,
        exit_reason_counts=exit_reason_counts,
        closed_trades=closed_trades,
    )

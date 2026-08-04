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

from core.backtest.costs import compute_round_trip_costs
from core.backtest.day_runner import BacktestDayResult
from core.execution.models import ExecutionSessionResult


@dataclass(slots=True)
class ClosedTrade:
    """
    ``realized_pnl`` is net of ``costs`` (brokerage + STT + exchange/other
    charges - see ``core/backtest/costs.py``) - what this trade actually
    made/lost, not the gross price move. ``costs`` never includes
    slippage, which is already baked into ``entry_price``/``exit_price``
    themselves by ``core/broker/paper.py``.
    """

    trading_date: date
    symbol: str
    exchange: str
    quantity: int
    entry_price: float
    exit_price: float
    exit_reason: str | None
    realized_pnl: float
    costs: float = 0.0


@dataclass(slots=True)
class BacktestPerformanceReport:
    trading_days: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    total_realized_pnl: float
    total_costs: float
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
    # Accumulated across the WHOLE day_results list, not reset per day:
    # an entry and its matching exit can land on different
    # BacktestDayResults for a multi-day-holding strategy (see
    # core.backtest.multi_day_position_runner) - identical result for
    # every existing intraday strategy either way, since a queue that
    # already drains to empty by day's end (entries/exits always
    # alternate, at most one open lot per symbol - see this module's
    # docstring) behaves the same whether or not it's reset in between.
    open_entries: dict[str, deque] = defaultdict(deque)

    for day_result in day_results:
        result = day_result.session_result
        duplicate_orders_blocked += result.duplicate_orders_blocked

        for order in result.placed_orders:
            open_entries[f"{order.exchange}:{order.symbol}"].append(order)

        for exit_decision, exit_order in zip(result.exit_decisions, result.exit_orders):
            queue = open_entries[f"{exit_decision.exchange}:{exit_decision.symbol}"]
            if not queue:
                continue  # no matching entry recorded - shouldn't happen; skip defensively
            entry_order = queue.popleft()
            if entry_order.average_price is None or exit_order.average_price is None:
                continue

            costs = compute_round_trip_costs(
                entry_order.average_price, exit_order.average_price, exit_decision.quantity
            )
            gross_pnl = (exit_order.average_price - entry_order.average_price) * exit_decision.quantity
            closed_trades.append(
                ClosedTrade(
                    trading_date=day_result.trading_date,
                    symbol=exit_decision.symbol,
                    exchange=exit_decision.exchange,
                    quantity=exit_decision.quantity,
                    entry_price=entry_order.average_price,
                    exit_price=exit_order.average_price,
                    exit_reason=exit_decision.reason,
                    realized_pnl=gross_pnl - costs,
                    costs=costs,
                )
            )

    winning = [t for t in closed_trades if t.realized_pnl > 0]
    losing = [t for t in closed_trades if t.realized_pnl < 0]
    breakeven = [t for t in closed_trades if t.realized_pnl == 0]

    total_pnl = sum(t.realized_pnl for t in closed_trades)
    total_costs = sum(t.costs for t in closed_trades)
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
        total_costs=total_costs,
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


def build_combined_day_results(
    results_by_strategy: dict[str, list[BacktestDayResult]],
) -> list[BacktestDayResult]:
    """
    Flatten a multi-strategy portfolio backtest's per-strategy day results
    (see ``core.backtest.portfolio_day_runner.run_multi_day_portfolio_backtest``)
    into ONE combined ``list[BacktestDayResult]``, so the existing
    ``build_performance_report`` (built for a single strategy's day
    results) can be reused unmodified to produce a portfolio-level report -
    "the whole portfolio" is just every strategy's orders pooled together.

    Every strategy's day list must be the same length and share the same
    trading_date at each index - guaranteed by
    ``run_multi_day_portfolio_backtest``, which runs every active strategy
    through the identical shared day sequence (a strategy that traded
    nothing on a given day still has an (empty) entry for it, never a
    missing one).

    Within a day, orders are concatenated in ``results_by_strategy``'s
    iteration order (i.e. the active-portfolio config's declared order) -
    ``ClosedTrade`` only carries a ``trading_date``, not an intra-day
    timestamp, so true cross-strategy chronological interleaving within one
    day isn't reconstructable from this structure. This only affects the
    combined equity curve's intra-day drawdown path, never total PnL.
    """
    strategy_names = list(results_by_strategy)
    if not strategy_names:
        raise ValueError("results_by_strategy must not be empty")
    day_counts = {len(results_by_strategy[name]) for name in strategy_names}
    if len(day_counts) != 1:
        raise ValueError(
            f"every strategy must have the same number of trading days, got {day_counts}"
        )

    combined: list[BacktestDayResult] = []
    for day_results in zip(*(results_by_strategy[name] for name in strategy_names)):
        trading_date = day_results[0].trading_date
        if any(day.trading_date != trading_date for day in day_results):
            raise ValueError("strategies' day results are not aligned to the same trading_date sequence")

        combined_result = ExecutionSessionResult(trading_date=trading_date, strategy_name="portfolio")
        for day in day_results:
            result = day.session_result
            combined_result.processed_bars += result.processed_bars
            combined_result.generated_signals += result.generated_signals
            combined_result.approved_signals += result.approved_signals
            combined_result.rejected_signals += result.rejected_signals
            combined_result.skipped_due_to_session_guardrails += result.skipped_due_to_session_guardrails
            combined_result.skipped_due_to_position_limits += result.skipped_due_to_position_limits
            combined_result.duplicate_orders_blocked += result.duplicate_orders_blocked
            combined_result.errors_encountered += result.errors_encountered
            combined_result.placed_orders.extend(result.placed_orders)
            combined_result.exit_decisions.extend(result.exit_decisions)
            combined_result.exit_orders.extend(result.exit_orders)
            combined_result.symbols_with_signals.update(result.symbols_with_signals)

        combined.append(BacktestDayResult(trading_date=trading_date, session_result=combined_result))

    return combined

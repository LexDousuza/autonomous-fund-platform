"""
Unit tests for core.backtest.report.

Constructs ExecutionSessionResult objects directly (no need to run a
real orchestrator session) to exercise the FIFO entry/exit pairing and
statistics computation precisely.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_backtest_report.py -v
"""
from __future__ import annotations

import unittest
from datetime import date

from core.backtest.day_runner import BacktestDayResult
from core.backtest.report import build_performance_report
from core.broker.base import Order, OrderSide, OrderStatus, OrderType, ProductType
from core.execution.models import ExecutionSessionResult
from core.portfolio.exit_models import ExitDecision


def _order(symbol: str, average_price: float, quantity: int = 10, side: OrderSide = OrderSide.BUY) -> Order:
    return Order(
        order_id=f"order-{symbol}-{average_price}",
        symbol=symbol,
        exchange="NSE",
        side=side,
        quantity=quantity,
        filled_quantity=quantity,
        status=OrderStatus.FILLED,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        average_price=average_price,
    )


def _exit_decision(symbol: str, quantity: int = 10, reason: str = "target") -> ExitDecision:
    return ExitDecision(should_exit=True, symbol=symbol, exchange="NSE", quantity=quantity, reason=reason)


def _day_result(
    trading_date: date,
    placed_orders: list[Order],
    exit_decisions: list[ExitDecision],
    exit_orders: list[Order],
    duplicate_orders_blocked: int = 0,
) -> BacktestDayResult:
    result = ExecutionSessionResult(
        trading_date=trading_date,
        strategy_name="test_strategy",
        placed_orders=placed_orders,
        exit_decisions=exit_decisions,
        exit_orders=exit_orders,
        duplicate_orders_blocked=duplicate_orders_blocked,
    )
    return BacktestDayResult(trading_date=trading_date, session_result=result)


class BuildPerformanceReportTests(unittest.TestCase):
    def test_single_winning_trade(self) -> None:
        day = _day_result(
            date(2026, 1, 5),
            placed_orders=[_order("RELIANCE", 100.0)],
            exit_decisions=[_exit_decision("RELIANCE", reason="target")],
            exit_orders=[_order("RELIANCE", 110.0, side=OrderSide.SELL)],
        )

        report = build_performance_report([day])

        self.assertEqual(report.total_trades, 1)
        self.assertEqual(report.winning_trades, 1)
        self.assertEqual(report.losing_trades, 0)
        self.assertEqual(report.total_realized_pnl, 100.0)  # (110-100)*10
        self.assertEqual(report.win_rate, 1.0)
        self.assertIsNone(report.profit_factor)  # no losses -> undefined, not infinity

    def test_single_losing_trade(self) -> None:
        day = _day_result(
            date(2026, 1, 5),
            placed_orders=[_order("RELIANCE", 100.0)],
            exit_decisions=[_exit_decision("RELIANCE", reason="stop_loss")],
            exit_orders=[_order("RELIANCE", 95.0, side=OrderSide.SELL)],
        )

        report = build_performance_report([day])

        self.assertEqual(report.losing_trades, 1)
        self.assertEqual(report.winning_trades, 0)
        self.assertEqual(report.total_realized_pnl, -50.0)  # (95-100)*10
        self.assertEqual(report.win_rate, 0.0)
        self.assertEqual(report.profit_factor, 0.0)  # zero profit against a real loss -> well-defined 0

    def test_win_rate_and_profit_factor_across_mixed_trades(self) -> None:
        day = _day_result(
            date(2026, 1, 5),
            placed_orders=[_order("A", 100.0), _order("B", 100.0)],
            exit_decisions=[_exit_decision("A", reason="target"), _exit_decision("B", reason="stop_loss")],
            exit_orders=[
                _order("A", 120.0, side=OrderSide.SELL),  # +200
                _order("B", 90.0, side=OrderSide.SELL),  # -100
            ],
        )

        report = build_performance_report([day])

        self.assertEqual(report.total_trades, 2)
        self.assertEqual(report.win_rate, 0.5)
        self.assertEqual(report.total_realized_pnl, 100.0)
        self.assertAlmostEqual(report.profit_factor, 200.0 / 100.0)
        self.assertEqual(report.average_win, 200.0)
        self.assertEqual(report.average_loss, -100.0)

    def test_fifo_pairs_entries_and_exits_per_symbol_across_a_day(self) -> None:
        # Two round-trips on the same symbol within one day: entry1/exit1
        # then entry2/exit2 - must pair in order, not cross-match.
        day = _day_result(
            date(2026, 1, 5),
            placed_orders=[_order("RELIANCE", 100.0), _order("RELIANCE", 200.0)],
            exit_decisions=[_exit_decision("RELIANCE"), _exit_decision("RELIANCE")],
            exit_orders=[
                _order("RELIANCE", 110.0, side=OrderSide.SELL),
                _order("RELIANCE", 190.0, side=OrderSide.SELL),
            ],
        )

        report = build_performance_report([day])

        pnls = sorted(t.realized_pnl for t in report.closed_trades)
        self.assertEqual(pnls, [-100.0, 100.0])  # (190-200)*10, (110-100)*10

    def test_exit_reason_counts_are_tallied(self) -> None:
        day = _day_result(
            date(2026, 1, 5),
            placed_orders=[_order("A", 100.0), _order("B", 100.0)],
            exit_decisions=[_exit_decision("A", reason="target"), _exit_decision("B", reason="target")],
            exit_orders=[
                _order("A", 110.0, side=OrderSide.SELL),
                _order("B", 110.0, side=OrderSide.SELL),
            ],
        )

        report = build_performance_report([day])

        self.assertEqual(report.exit_reason_counts, {"target": 2})

    def test_max_drawdown_over_a_losing_then_recovering_sequence(self) -> None:
        # PnL sequence: +100, -150, -50, +200 -> cumulative: 100, -50, -100, 100
        # Peak before the trough is 100; trough is -100 -> drawdown 200.
        day = _day_result(
            date(2026, 1, 5),
            placed_orders=[_order("A", 100.0), _order("B", 100.0), _order("C", 100.0), _order("D", 100.0)],
            exit_decisions=[_exit_decision("A"), _exit_decision("B"), _exit_decision("C"), _exit_decision("D")],
            exit_orders=[
                _order("A", 110.0, side=OrderSide.SELL),  # +100
                _order("B", 85.0, side=OrderSide.SELL),  # -150
                _order("C", 95.0, side=OrderSide.SELL),  # -50
                _order("D", 120.0, side=OrderSide.SELL),  # +200
            ],
        )

        report = build_performance_report([day])

        self.assertEqual(report.max_drawdown, 200.0)

    def test_duplicate_orders_blocked_is_summed_across_days(self) -> None:
        day1 = _day_result(date(2026, 1, 5), [], [], [], duplicate_orders_blocked=2)
        day2 = _day_result(date(2026, 1, 6), [], [], [], duplicate_orders_blocked=3)

        report = build_performance_report([day1, day2])

        self.assertEqual(report.duplicate_orders_blocked, 5)

    def test_no_trades_produces_a_safe_zeroed_report(self) -> None:
        day = _day_result(date(2026, 1, 5), [], [], [])

        report = build_performance_report([day])

        self.assertEqual(report.total_trades, 0)
        self.assertEqual(report.win_rate, 0.0)
        self.assertEqual(report.average_pnl_per_trade, 0.0)
        self.assertIsNone(report.profit_factor)


if __name__ == "__main__":
    unittest.main()

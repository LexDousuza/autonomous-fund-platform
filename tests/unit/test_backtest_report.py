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

from core.backtest.costs import compute_round_trip_costs
from core.backtest.day_runner import BacktestDayResult
from core.backtest.report import build_combined_day_results, build_performance_report
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
        expected_net_pnl = 100.0 - compute_round_trip_costs(100.0, 110.0, 10)  # (110-100)*10, net of costs

        self.assertEqual(report.total_trades, 1)
        self.assertEqual(report.winning_trades, 1)
        self.assertEqual(report.losing_trades, 0)
        self.assertAlmostEqual(report.total_realized_pnl, expected_net_pnl)
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
        expected_net_pnl = -50.0 - compute_round_trip_costs(100.0, 95.0, 10)  # (95-100)*10, net of costs

        self.assertEqual(report.losing_trades, 1)
        self.assertEqual(report.winning_trades, 0)
        self.assertAlmostEqual(report.total_realized_pnl, expected_net_pnl)
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
        net_win = 200.0 - compute_round_trip_costs(100.0, 120.0, 10)
        net_loss = -100.0 - compute_round_trip_costs(100.0, 90.0, 10)

        self.assertEqual(report.total_trades, 2)
        self.assertEqual(report.win_rate, 0.5)
        self.assertAlmostEqual(report.total_realized_pnl, net_win + net_loss)
        self.assertAlmostEqual(report.profit_factor, net_win / abs(net_loss))
        self.assertAlmostEqual(report.average_win, net_win)
        self.assertAlmostEqual(report.average_loss, net_loss)

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
        expected = sorted(
            [
                100.0 - compute_round_trip_costs(100.0, 110.0, 10),  # (110-100)*10, net of costs
                -100.0 - compute_round_trip_costs(200.0, 190.0, 10),  # (190-200)*10, net of costs
            ]
        )

        pnls = sorted(t.realized_pnl for t in report.closed_trades)
        for actual, exp in zip(pnls, expected):
            self.assertAlmostEqual(actual, exp)

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
        # Gross PnL sequence: +100, -150, -50, +200 (each also net of its
        # own round-trip costs now - see core/backtest/costs.py). Expected
        # drawdown is derived from the same net figures via the same
        # peak-vs-cumulative logic build_performance_report uses, so this
        # test still verifies the *sequencing* logic, not a specific cost
        # figure.
        day = _day_result(
            date(2026, 1, 5),
            placed_orders=[_order("A", 100.0), _order("B", 100.0), _order("C", 100.0), _order("D", 100.0)],
            exit_decisions=[_exit_decision("A"), _exit_decision("B"), _exit_decision("C"), _exit_decision("D")],
            exit_orders=[
                _order("A", 110.0, side=OrderSide.SELL),  # +100 gross
                _order("B", 85.0, side=OrderSide.SELL),  # -150 gross
                _order("C", 95.0, side=OrderSide.SELL),  # -50 gross
                _order("D", 120.0, side=OrderSide.SELL),  # +200 gross
            ],
        )

        report = build_performance_report([day])

        net_pnls = [
            100.0 - compute_round_trip_costs(100.0, 110.0, 10),
            -150.0 - compute_round_trip_costs(100.0, 85.0, 10),
            -50.0 - compute_round_trip_costs(100.0, 95.0, 10),
            200.0 - compute_round_trip_costs(100.0, 120.0, 10),
        ]
        cumulative = peak = expected_drawdown = 0.0
        for pnl in net_pnls:
            cumulative += pnl
            peak = max(peak, cumulative)
            expected_drawdown = max(expected_drawdown, peak - cumulative)

        self.assertAlmostEqual(report.max_drawdown, expected_drawdown)

    def test_duplicate_orders_blocked_is_summed_across_days(self) -> None:
        day1 = _day_result(date(2026, 1, 5), [], [], [], duplicate_orders_blocked=2)
        day2 = _day_result(date(2026, 1, 6), [], [], [], duplicate_orders_blocked=3)

        report = build_performance_report([day1, day2])

        self.assertEqual(report.duplicate_orders_blocked, 5)

    def test_pairs_an_entry_and_exit_that_land_on_different_days(self) -> None:
        # core.backtest.multi_day_position_runner (a multi-day-holding
        # strategy, e.g. vertical_credit_spread) can place an entry on
        # one BacktestDayResult and its matching exit on a LATER one -
        # every existing intraday strategy always closes same-day, so
        # this never mattered before. Without cross-day accumulation,
        # the exit's per-day-local open_entries queue would be empty
        # (that day placed nothing) and the trade would be silently
        # dropped instead of counted.
        day1 = _day_result(date(2026, 1, 5), placed_orders=[_order("NIFTY_OPT", 100.0)], exit_decisions=[], exit_orders=[])
        day2 = _day_result(date(2026, 1, 6), placed_orders=[], exit_decisions=[], exit_orders=[])
        day3 = _day_result(
            date(2026, 1, 8), placed_orders=[],
            exit_decisions=[_exit_decision("NIFTY_OPT", reason="max_holding_bars")],
            exit_orders=[_order("NIFTY_OPT", 110.0, side=OrderSide.SELL)],
        )

        report = build_performance_report([day1, day2, day3])

        self.assertEqual(report.total_trades, 1)
        self.assertEqual(report.closed_trades[0].trading_date, date(2026, 1, 8))  # attributed to the exit's own day
        expected_net_pnl = 100.0 - compute_round_trip_costs(100.0, 110.0, 10)
        self.assertAlmostEqual(report.total_realized_pnl, expected_net_pnl)

    def test_no_trades_produces_a_safe_zeroed_report(self) -> None:
        day = _day_result(date(2026, 1, 5), [], [], [])

        report = build_performance_report([day])

        self.assertEqual(report.total_trades, 0)
        self.assertEqual(report.win_rate, 0.0)
        self.assertEqual(report.average_pnl_per_trade, 0.0)
        self.assertIsNone(report.profit_factor)


class BuildCombinedDayResultsTests(unittest.TestCase):
    def test_pools_trades_from_every_strategy_by_day(self) -> None:
        strategy_a = [
            _day_result(
                date(2026, 1, 5),
                placed_orders=[_order("RELIANCE", 100.0)],
                exit_decisions=[_exit_decision("RELIANCE", reason="target")],
                exit_orders=[_order("RELIANCE", 110.0, side=OrderSide.SELL)],
            )
        ]
        strategy_b = [
            _day_result(
                date(2026, 1, 5),
                placed_orders=[_order("TCS", 200.0)],
                exit_decisions=[_exit_decision("TCS", reason="stop_loss")],
                exit_orders=[_order("TCS", 190.0, side=OrderSide.SELL)],
            )
        ]

        combined = build_combined_day_results({"momentum": strategy_a, "pullback": strategy_b})
        report = build_performance_report(combined)

        self.assertEqual(len(combined), 1)
        self.assertEqual(report.total_trades, 2)
        self.assertEqual(report.winning_trades, 1)
        self.assertEqual(report.losing_trades, 1)

    def test_matches_sum_of_independently_built_reports(self) -> None:
        strategy_a = [
            _day_result(
                date(2026, 1, 5),
                placed_orders=[_order("A", 100.0)],
                exit_decisions=[_exit_decision("A")],
                exit_orders=[_order("A", 120.0, side=OrderSide.SELL)],
            )
        ]
        strategy_b = [
            _day_result(
                date(2026, 1, 5),
                placed_orders=[_order("B", 50.0)],
                exit_decisions=[_exit_decision("B")],
                exit_orders=[_order("B", 40.0, side=OrderSide.SELL)],
            )
        ]

        combined_report = build_performance_report(
            build_combined_day_results({"a": strategy_a, "b": strategy_b})
        )
        report_a = build_performance_report(strategy_a)
        report_b = build_performance_report(strategy_b)

        self.assertAlmostEqual(
            combined_report.total_realized_pnl,
            report_a.total_realized_pnl + report_b.total_realized_pnl,
        )
        self.assertEqual(combined_report.total_trades, report_a.total_trades + report_b.total_trades)

    def test_empty_input_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_combined_day_results({})

    def test_mismatched_day_counts_raise(self) -> None:
        strategy_a = [_day_result(date(2026, 1, 5), [], [], [])]
        strategy_b = [
            _day_result(date(2026, 1, 5), [], [], []),
            _day_result(date(2026, 1, 6), [], [], []),
        ]

        with self.assertRaises(ValueError):
            build_combined_day_results({"a": strategy_a, "b": strategy_b})

    def test_misaligned_trading_dates_raise(self) -> None:
        strategy_a = [_day_result(date(2026, 1, 5), [], [], [])]
        strategy_b = [_day_result(date(2026, 1, 6), [], [], [])]

        with self.assertRaises(ValueError):
            build_combined_day_results({"a": strategy_a, "b": strategy_b})


if __name__ == "__main__":
    unittest.main()

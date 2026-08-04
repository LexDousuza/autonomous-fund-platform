"""Unit tests for core.analytics.session_performance."""
from __future__ import annotations

import unittest

from core.analytics.session_performance import (
    compute_closed_trades,
    compute_cumulative_pnl_series,
    compute_portfolio_metrics,
)


def entry(symbol: str, price: float, quantity: int, ts: str) -> dict:
    return {
        "event_type": "order_placed",
        "timestamp": ts,
        "symbol": symbol,
        "payload": {"status": "FILLED", "average_price": price, "quantity": quantity},
    }


def exit_(symbol: str, price: float, quantity: int, ts: str) -> dict:
    return {
        "event_type": "exit_order_placed",
        "timestamp": ts,
        "symbol": symbol,
        "payload": {"status": "FILLED", "average_price": price, "quantity": quantity},
    }


class ComputeClosedTradesTests(unittest.TestCase):
    def test_empty_events(self) -> None:
        self.assertEqual(compute_closed_trades([]), [])

    def test_matches_profit_and_loss(self) -> None:
        events = [
            entry("RELIANCE", 1000.0, 10, "2026-07-13T08:00:00+00:00"),
            exit_("RELIANCE", 1050.0, 10, "2026-07-13T08:05:00+00:00"),
            entry("TCS", 500.0, 5, "2026-07-13T08:10:00+00:00"),
            exit_("TCS", 480.0, 5, "2026-07-13T08:15:00+00:00"),
        ]
        trades = compute_closed_trades(events)

        self.assertEqual(len(trades), 2)
        self.assertAlmostEqual(trades[0].pnl, 500.0)
        self.assertAlmostEqual(trades[1].pnl, -100.0)

    def test_unmatched_exit_is_skipped(self) -> None:
        events = [exit_("RELIANCE", 1050.0, 10, "2026-07-13T08:05:00+00:00")]

        self.assertEqual(compute_closed_trades(events), [])

    def test_fifo_matching_per_symbol(self) -> None:
        events = [
            entry("RELIANCE", 1000.0, 10, "2026-07-13T08:00:00+00:00"),
            entry("RELIANCE", 1010.0, 10, "2026-07-13T08:01:00+00:00"),
            exit_("RELIANCE", 1050.0, 10, "2026-07-13T08:05:00+00:00"),
        ]
        trades = compute_closed_trades(events)

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].entry_price, 1000.0)

    def test_non_filled_orders_ignored(self) -> None:
        events = [
            {
                "event_type": "order_placed",
                "timestamp": "2026-07-13T08:00:00+00:00",
                "symbol": "RELIANCE",
                "payload": {"status": "REJECTED", "average_price": 1000.0, "quantity": 10},
            },
            exit_("RELIANCE", 1050.0, 10, "2026-07-13T08:05:00+00:00"),
        ]

        self.assertEqual(compute_closed_trades(events), [])


class ComputeCumulativePnlSeriesTests(unittest.TestCase):
    def test_running_total_in_exit_order(self) -> None:
        events = [
            entry("RELIANCE", 1000.0, 10, "2026-07-13T08:00:00+00:00"),
            exit_("RELIANCE", 1050.0, 10, "2026-07-13T08:05:00+00:00"),
            entry("TCS", 500.0, 5, "2026-07-13T08:10:00+00:00"),
            exit_("TCS", 480.0, 5, "2026-07-13T08:15:00+00:00"),
        ]
        series = compute_cumulative_pnl_series(compute_closed_trades(events))

        self.assertEqual([round(v, 2) for _, v in series], [500.0, 400.0])

    def test_empty_trades_gives_empty_series(self) -> None:
        self.assertEqual(compute_cumulative_pnl_series([]), [])


class ComputePortfolioMetricsTests(unittest.TestCase):
    def test_no_trades_gives_zeros_and_nones(self) -> None:
        metrics = compute_portfolio_metrics([])

        self.assertEqual(metrics.trade_count, 0)
        self.assertEqual(metrics.total_pnl, 0.0)
        self.assertEqual(metrics.max_drawdown, 0.0)
        self.assertIsNone(metrics.win_rate)
        self.assertIsNone(metrics.profit_factor)
        self.assertIsNone(metrics.sharpe_ratio)

    def test_win_rate_and_profit_factor_from_a_mix(self) -> None:
        events = [
            entry("RELIANCE", 1000.0, 10, "2026-07-13T08:00:00+00:00"),
            exit_("RELIANCE", 1100.0, 10, "2026-07-13T08:05:00+00:00"),  # +1000
            entry("TCS", 500.0, 10, "2026-07-13T08:10:00+00:00"),
            exit_("TCS", 450.0, 10, "2026-07-13T08:15:00+00:00"),  # -500
            entry("INFY", 200.0, 10, "2026-07-13T08:20:00+00:00"),
            exit_("INFY", 250.0, 10, "2026-07-13T08:25:00+00:00"),  # +500
        ]
        metrics = compute_portfolio_metrics(compute_closed_trades(events))

        self.assertEqual(metrics.trade_count, 3)
        self.assertAlmostEqual(metrics.total_pnl, 1000.0)
        self.assertAlmostEqual(metrics.win_rate, 2 / 3)
        self.assertAlmostEqual(metrics.profit_factor, 1500.0 / 500.0)
        self.assertAlmostEqual(metrics.avg_win, 750.0)
        self.assertAlmostEqual(metrics.avg_loss, -500.0)

    def test_all_wins_has_no_profit_factor_or_avg_loss(self) -> None:
        events = [
            entry("RELIANCE", 1000.0, 10, "2026-07-13T08:00:00+00:00"),
            exit_("RELIANCE", 1100.0, 10, "2026-07-13T08:05:00+00:00"),
        ]
        metrics = compute_portfolio_metrics(compute_closed_trades(events))

        self.assertIsNone(metrics.profit_factor)
        self.assertIsNone(metrics.avg_loss)

    def test_sharpe_is_none_below_two_trades(self) -> None:
        events = [
            entry("RELIANCE", 1000.0, 10, "2026-07-13T08:00:00+00:00"),
            exit_("RELIANCE", 1100.0, 10, "2026-07-13T08:05:00+00:00"),
        ]
        metrics = compute_portfolio_metrics(compute_closed_trades(events))

        self.assertIsNone(metrics.sharpe_ratio)

    def test_max_drawdown_from_peak_to_trough(self) -> None:
        # Running equity: +1000, +1500 (peak), +900 (trough, dd=600), +1200
        events = [
            entry("A", 100.0, 10, "2026-07-13T08:00:00+00:00"),
            exit_("A", 200.0, 10, "2026-07-13T08:01:00+00:00"),  # +1000
            entry("B", 100.0, 10, "2026-07-13T08:02:00+00:00"),
            exit_("B", 150.0, 10, "2026-07-13T08:03:00+00:00"),  # +500 -> 1500 peak
            entry("C", 100.0, 10, "2026-07-13T08:04:00+00:00"),
            exit_("C", 40.0, 10, "2026-07-13T08:05:00+00:00"),  # -600 -> 900 trough
            entry("D", 100.0, 10, "2026-07-13T08:06:00+00:00"),
            exit_("D", 130.0, 10, "2026-07-13T08:07:00+00:00"),  # +300 -> 1200
        ]
        metrics = compute_portfolio_metrics(compute_closed_trades(events))

        self.assertAlmostEqual(metrics.max_drawdown, 600.0)


if __name__ == "__main__":
    unittest.main()

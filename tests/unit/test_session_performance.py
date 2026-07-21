"""Unit tests for core.analytics.session_performance."""
from __future__ import annotations

import unittest

from core.analytics.session_performance import (
    compute_closed_trades,
    compute_cumulative_pnl_series,
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


if __name__ == "__main__":
    unittest.main()

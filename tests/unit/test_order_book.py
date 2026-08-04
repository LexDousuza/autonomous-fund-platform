"""
Unit tests for core.analytics.order_book.compute_order_records.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_order_book.py -v
"""
from __future__ import annotations

import unittest

from core.analytics.order_book import compute_order_records


def _event(event_type: str, symbol: str, timestamp: str, payload: dict) -> dict:
    return {"event_type": event_type, "symbol": symbol, "timestamp": timestamp, "payload": payload}


class ComputeOrderRecordsTests(unittest.TestCase):
    def test_matches_submitted_to_placed_with_latency_and_slippage(self) -> None:
        events = [
            _event("order_submitted", "TEST", "2026-01-01T09:00:00+00:00",
                   {"side": "BUY", "quantity": 10, "order_type": "LIMIT", "price": 100.0}),
            _event("order_placed", "TEST", "2026-01-01T09:00:00.250000+00:00",
                   {"order_id": "abc", "status": "FILLED", "average_price": 100.5}),
        ]

        records = compute_order_records(events)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.order_id, "abc")
        self.assertEqual(record.side, "BUY")
        self.assertEqual(record.intended_price, 100.0)
        self.assertEqual(record.average_price, 100.5)
        self.assertAlmostEqual(record.slippage, 0.5)
        self.assertAlmostEqual(record.latency_ms, 250.0)

    def test_rejected_order_has_no_slippage(self) -> None:
        events = [
            _event("order_submitted", "TEST", "2026-01-01T09:00:00+00:00",
                   {"side": "BUY", "quantity": 10, "order_type": "LIMIT", "price": 100.0}),
            _event("order_rejected", "TEST", "2026-01-01T09:00:01+00:00",
                   {"order_id": "def", "status": "REJECTED", "reference_id": "x"}),
        ]

        records = compute_order_records(events)

        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].average_price)
        self.assertIsNone(records[0].slippage)

    def test_response_with_no_matching_submission_is_skipped(self) -> None:
        events = [
            _event("order_placed", "TEST", "2026-01-01T09:00:00+00:00", {"order_id": "orphan"}),
        ]

        self.assertEqual(compute_order_records(events), [])

    def test_exit_order_placed_with_no_matching_exit_triggered_is_skipped(self) -> None:
        events = [
            _event("exit_order_placed", "TEST", "2026-01-01T09:00:00+00:00", {"order_id": "orphan-exit"}),
        ]

        self.assertEqual(compute_order_records(events), [])

    def test_entry_and_exit_legs_are_matched_independently(self) -> None:
        # Exits never emit order_submitted (core/execution/signal_pipeline.py's
        # process_exit goes straight from exit_triggered to exit_order_placed) -
        # this proves both leg types are tracked, not just entries.
        events = [
            _event("order_submitted", "TEST", "2026-01-01T09:00:00+00:00", {"side": "BUY", "quantity": 5, "price": 100.0}),
            _event("order_placed", "TEST", "2026-01-01T09:00:01+00:00", {"order_id": "first", "status": "FILLED", "average_price": 100.0}),
            _event("exit_triggered", "TEST", "2026-01-01T09:10:00+00:00", {"reason": "target", "quantity": 5, "exit_price": 105.0}),
            _event("exit_order_placed", "TEST", "2026-01-01T09:10:01+00:00", {"order_id": "second", "status": "FILLED", "average_price": 104.5}),
        ]

        records = compute_order_records(events)

        self.assertEqual([r.order_id for r in records], ["first", "second"])
        self.assertEqual(records[1].side, "SELL")
        self.assertEqual(records[1].intended_price, 105.0)
        self.assertAlmostEqual(records[1].slippage, -0.5)


if __name__ == "__main__":
    unittest.main()

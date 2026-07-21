"""
Unit tests for core.execution.idempotency.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_idempotency.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime

from core.broker.base import OrderSide
from core.execution.idempotency import OrderIdempotencyGuard, build_order_reference_id

TS = datetime(2026, 1, 1, 9, 15, 0)


class BuildOrderReferenceIdTests(unittest.TestCase):
    def test_same_inputs_produce_the_same_id(self) -> None:
        id_a = build_order_reference_id("simple_breakout_momentum", "NSE", "RELIANCE", OrderSide.BUY, TS, "entry")
        id_b = build_order_reference_id("simple_breakout_momentum", "NSE", "RELIANCE", OrderSide.BUY, TS, "entry")

        self.assertEqual(id_a, id_b)

    def test_different_symbol_produces_a_different_id(self) -> None:
        id_a = build_order_reference_id("s", "NSE", "RELIANCE", OrderSide.BUY, TS, "entry")
        id_b = build_order_reference_id("s", "NSE", "TCS", OrderSide.BUY, TS, "entry")

        self.assertNotEqual(id_a, id_b)

    def test_different_timestamp_produces_a_different_id(self) -> None:
        id_a = build_order_reference_id("s", "NSE", "RELIANCE", OrderSide.BUY, TS, "entry")
        id_b = build_order_reference_id("s", "NSE", "RELIANCE", OrderSide.BUY, datetime(2026, 1, 1, 9, 16, 0), "entry")

        self.assertNotEqual(id_a, id_b)

    def test_entry_and_exit_for_the_same_bar_never_collide(self) -> None:
        entry_id = build_order_reference_id("s", "NSE", "RELIANCE", OrderSide.BUY, TS, "entry")
        exit_id = build_order_reference_id("s", "NSE", "RELIANCE", OrderSide.SELL, TS, "exit:stop_loss")

        self.assertNotEqual(entry_id, exit_id)

    def test_id_is_short_and_fixed_length(self) -> None:
        reference_id = build_order_reference_id("s", "NSE", "RELIANCE", OrderSide.BUY, TS, "entry")

        self.assertEqual(len(reference_id), 16)


class OrderIdempotencyGuardTests(unittest.TestCase):
    def test_first_use_of_an_id_is_reported_as_new(self) -> None:
        guard = OrderIdempotencyGuard()

        self.assertTrue(guard.check_and_record("abc123"))

    def test_repeated_id_is_reported_as_a_duplicate(self) -> None:
        guard = OrderIdempotencyGuard()
        guard.check_and_record("abc123")

        self.assertFalse(guard.check_and_record("abc123"))

    def test_different_ids_are_tracked_independently(self) -> None:
        guard = OrderIdempotencyGuard()

        self.assertTrue(guard.check_and_record("abc123"))
        self.assertTrue(guard.check_and_record("def456"))
        self.assertFalse(guard.check_and_record("abc123"))
        self.assertFalse(guard.check_and_record("def456"))


if __name__ == "__main__":
    unittest.main()

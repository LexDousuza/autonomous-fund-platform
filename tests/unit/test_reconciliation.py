"""
Unit tests for core.execution.reconciliation.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_reconciliation.py -v
"""
from __future__ import annotations

import unittest

from core.audit.models import AuditEvent, AuditEventType
from core.audit.sink import AuditSink
from core.broker.base import Position, ProductType
from core.broker.paper import PaperBroker
from core.execution.reconciliation import check_broker_reconciliation


class InMemoryAuditSink(AuditSink):
    """Test double: records everything in memory, no file I/O."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def start_session(self, metadata) -> None:  # noqa: ANN001 - unused in these tests
        pass

    def record_event(self, event: AuditEvent) -> None:
        self.events.append(event)

    def finalize_session(self, summary) -> None:  # noqa: ANN001 - unused in these tests
        pass

    def record_failure(self, error: BaseException) -> None:
        pass

    def events_of_type(self, event_type: AuditEventType) -> list[AuditEvent]:
        return [e for e in self.events if e.event_type == event_type]


class FakeBroker:
    """Minimal Broker-shaped double: only list_positions matters here."""

    def __init__(self, positions: list[Position]) -> None:
        self._positions = positions

    def place_order(self, order_request):  # pragma: no cover - unused
        raise NotImplementedError

    def cancel_order(self, order_id):  # pragma: no cover - unused
        raise NotImplementedError

    def get_order(self, order_id):  # pragma: no cover - unused
        raise NotImplementedError

    def list_positions(self) -> list[Position]:
        return self._positions


def _position(symbol: str, quantity: int) -> Position:
    return Position(
        symbol=symbol,
        exchange="NSE",
        quantity=quantity,
        average_price=100.0,
        product_type=ProductType.INTRADAY,
    )


class CheckBrokerReconciliationTests(unittest.TestCase):
    def test_fresh_paper_broker_is_clean(self) -> None:
        result = check_broker_reconciliation(PaperBroker())

        self.assertTrue(result.is_clean)
        self.assertEqual(result.unexpected_positions, [])

    def test_broker_with_open_positions_is_not_clean(self) -> None:
        broker = FakeBroker([_position("RELIANCE", 10)])

        result = check_broker_reconciliation(broker)  # type: ignore[arg-type]

        self.assertFalse(result.is_clean)
        self.assertEqual(len(result.unexpected_positions), 1)
        self.assertEqual(result.unexpected_positions[0].symbol, "RELIANCE")

    def test_zero_quantity_positions_are_not_unexpected(self) -> None:
        broker = FakeBroker([_position("RELIANCE", 0), _position("TCS", 0)])

        result = check_broker_reconciliation(broker)  # type: ignore[arg-type]

        self.assertTrue(result.is_clean)

    def test_short_positions_also_count_as_unexpected(self) -> None:
        broker = FakeBroker([_position("RELIANCE", -5)])

        result = check_broker_reconciliation(broker)  # type: ignore[arg-type]

        self.assertFalse(result.is_clean)

    def test_mixed_positions_only_reports_nonzero_ones(self) -> None:
        broker = FakeBroker([_position("RELIANCE", 10), _position("TCS", 0), _position("INFY", -3)])

        result = check_broker_reconciliation(broker)  # type: ignore[arg-type]

        symbols = {p.symbol for p in result.unexpected_positions}
        self.assertEqual(symbols, {"RELIANCE", "INFY"})

    def test_clean_check_emits_checked_event_only(self) -> None:
        sink = InMemoryAuditSink()

        check_broker_reconciliation(PaperBroker(), audit_sink=sink)

        event_types = [e.event_type for e in sink.events]
        self.assertEqual(event_types, [AuditEventType.RECONCILIATION_CHECKED])
        self.assertTrue(sink.events[0].payload["clean"])
        self.assertEqual(sink.events[0].payload["unexpected_position_count"], 0)

    def test_dirty_check_emits_checked_and_mismatch_events(self) -> None:
        broker = FakeBroker([_position("RELIANCE", 10)])
        sink = InMemoryAuditSink()

        check_broker_reconciliation(broker, audit_sink=sink)  # type: ignore[arg-type]

        event_types = [e.event_type for e in sink.events]
        self.assertEqual(
            event_types, [AuditEventType.RECONCILIATION_CHECKED, AuditEventType.RECONCILIATION_MISMATCH]
        )
        self.assertFalse(sink.events[0].payload["clean"])
        mismatch_positions = sink.events[1].payload["positions"]
        self.assertEqual(mismatch_positions[0]["symbol"], "RELIANCE")
        self.assertEqual(mismatch_positions[0]["quantity"], 10)

    def test_no_audit_sink_is_a_safe_noop(self) -> None:
        broker = FakeBroker([_position("RELIANCE", 10)])

        result = check_broker_reconciliation(broker)  # type: ignore[arg-type]  # must not raise

        self.assertFalse(result.is_clean)


if __name__ == "__main__":
    unittest.main()

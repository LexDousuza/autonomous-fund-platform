"""
Unit tests for core.audit.file_sink (and core.audit.serialization's
redaction behavior, exercised through the sink).

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_audit_file_sink.py -v
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from core.audit.file_sink import FileAuditSink, find_unclean_prior_session
from core.audit.models import AuditEvent, AuditEventType, SessionMetadata, SessionSummary
from core.broker.base import Order, OrderSide, OrderStatus, OrderType, ProductType
from core.broker.groww_errors import GrowwTransportError
from core.broker.kite_errors import KiteTransportError
from core.market_data.errors import MarketDataTransportError


def make_metadata(session_id: str = "test-session-1", **overrides) -> SessionMetadata:
    defaults = dict(
        session_id=session_id,
        app_name="live_trader",
        started_at=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
        broker_provider="groww",
        broker_mode="live",
        market_data_provider="groww",
        market_data_mode="live",
        subscribed_symbols=["RELIANCE", "TCS"],
    )
    defaults.update(overrides)
    return SessionMetadata(**defaults)


def make_summary(succeeded: bool = True) -> SessionSummary:
    return SessionSummary(
        strategy_name="simple_breakout_momentum",
        trading_date=date(2026, 1, 1),
        processed_bars=3,
        generated_signals=1,
        symbols_with_signals=["RELIANCE"],
        approved_signals=1,
        rejected_signals=0,
        skipped_due_to_session_guardrails=0,
        skipped_due_to_position_limits=0,
        placed_orders=[
            Order(
                order_id="ord-1",
                symbol="RELIANCE",
                exchange="NSE",
                side=OrderSide.BUY,
                quantity=5,
                filled_quantity=5,
                status=OrderStatus.FILLED,
                order_type=OrderType.LIMIT,
                product_type=ProductType.INTRADAY,
                average_price=2501.0,
            )
        ],
        exit_decisions=[],
        exit_orders=[],
        final_positions=[],
        final_session_risk_state=None,
        succeeded=succeeded,
    )


class FileAuditSinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="audit_test_"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.sink = FileAuditSink(base_dir=self.tmp_dir)

    def _session_dir(self, session_id: str) -> Path:
        return self.tmp_dir / session_id

    def _read_session_json(self, session_id: str) -> dict:
        return json.loads((self._session_dir(session_id) / "session.json").read_text())

    def _read_events(self, session_id: str) -> list[dict]:
        path = self._session_dir(session_id) / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_starting_a_session_creates_directory_and_metadata_file(self) -> None:
        self.sink.start_session(make_metadata("session-a"))

        session_dir = self._session_dir("session-a")
        self.assertTrue(session_dir.is_dir())
        self.assertTrue((session_dir / "session.json").exists())

        data = self._read_session_json("session-a")
        self.assertEqual(data["session_id"], "session-a")
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["metadata"]["broker_provider"], "groww")
        self.assertEqual(data["metadata"]["subscribed_symbols"], ["RELIANCE", "TCS"])
        self.assertIsNone(data["ended_at"])
        self.assertIsNone(data["summary"])

    def test_starting_a_session_records_session_started_event(self) -> None:
        self.sink.start_session(make_metadata("session-b"))

        events = self._read_events("session-b")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "session_started")
        self.assertEqual(events[0]["sequence"], 1)
        self.assertEqual(events[0]["session_id"], "session-b")

    def test_recording_events_appends_valid_jsonl_records(self) -> None:
        self.sink.start_session(make_metadata("session-c"))

        self.sink.record_event(
            AuditEvent(event_type=AuditEventType.SIGNAL_GENERATED, symbol="RELIANCE", payload={"side": "BUY"})
        )
        self.sink.record_event(
            AuditEvent(event_type=AuditEventType.SIGNAL_APPROVED, symbol="RELIANCE", payload={"quantity": 5})
        )

        events = self._read_events("session-c")
        # session_started (1) + the two recorded above
        self.assertEqual(len(events), 3)
        self.assertEqual(events[1]["event_type"], "signal_generated")
        self.assertEqual(events[1]["symbol"], "RELIANCE")
        self.assertEqual(events[1]["payload"], {"side": "BUY"})
        self.assertEqual(events[1]["sequence"], 2)
        self.assertEqual(events[2]["event_type"], "signal_approved")
        self.assertEqual(events[2]["sequence"], 3)

    def test_record_event_before_start_session_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self.sink.record_event(AuditEvent(event_type=AuditEventType.SIGNAL_GENERATED))

    def test_finalizing_a_session_writes_final_summary_state(self) -> None:
        self.sink.start_session(make_metadata("session-d"))

        self.sink.finalize_session(make_summary(succeeded=True))

        data = self._read_session_json("session-d")
        self.assertEqual(data["status"], "completed")
        self.assertIsNotNone(data["ended_at"])
        self.assertEqual(data["summary"]["approved_signals"], 1)
        self.assertEqual(data["summary"]["strategy_name"], "simple_breakout_momentum")
        self.assertEqual(len(data["summary"]["placed_orders"]), 1)
        self.assertEqual(data["summary"]["placed_orders"][0]["order_id"], "ord-1")
        self.assertEqual(data["summary"]["placed_orders"][0]["side"], "BUY")  # enum -> plain string

        events = self._read_events("session-d")
        event_types = [e["event_type"] for e in events]
        self.assertIn("session_completed", event_types)

    def test_recording_failure_writes_failure_event_and_failed_status(self) -> None:
        self.sink.start_session(make_metadata("session-e"))

        self.sink.record_failure(ValueError("something broke"))

        data = self._read_session_json("session-e")
        self.assertEqual(data["status"], "failed")
        self.assertIsNotNone(data["ended_at"])
        self.assertIsNotNone(data["failure"])
        self.assertEqual(data["failure"]["error_type"], "ValueError")
        self.assertEqual(data["failure"]["message"], "something broke")
        self.assertIn("something broke", data["failure"]["traceback"])

        events = self._read_events("session-e")
        event_types = [e["event_type"] for e in events]
        self.assertIn("session_failed", event_types)

    def test_recording_failure_classifies_broker_errors(self) -> None:
        self.sink.start_session(make_metadata("session-f"))

        self.sink.record_failure(GrowwTransportError("connection refused"))

        events = self._read_events("session-f")
        event_types = [e["event_type"] for e in events]
        self.assertIn("broker_error", event_types)

    def test_recording_failure_classifies_kite_errors_as_broker_error(self) -> None:
        self.sink.start_session(make_metadata("session-f2"))

        self.sink.record_failure(KiteTransportError("connection refused"))

        events = self._read_events("session-f2")
        event_types = [e["event_type"] for e in events]
        self.assertIn("broker_error", event_types)

    def test_recording_failure_classifies_market_data_errors(self) -> None:
        self.sink.start_session(make_metadata("session-g"))

        self.sink.record_failure(MarketDataTransportError("bad quote response"))

        events = self._read_events("session-g")
        event_types = [e["event_type"] for e in events]
        self.assertIn("market_data_error", event_types)

    def test_recording_failure_classifies_unknown_errors_as_orchestrator_error(self) -> None:
        self.sink.start_session(make_metadata("session-h"))

        self.sink.record_failure(RuntimeError("unexpected"))

        events = self._read_events("session-h")
        event_types = [e["event_type"] for e in events]
        self.assertIn("orchestrator_error", event_types)

    def test_session_directory_remains_usable_after_failure(self) -> None:
        self.sink.start_session(make_metadata("session-i"))
        self.sink.record_event(AuditEvent(event_type=AuditEventType.SIGNAL_GENERATED, symbol="RELIANCE"))

        self.sink.record_failure(RuntimeError("crashed mid-session"))

        # Everything recorded before the crash is still readable.
        events = self._read_events("session-i")
        event_types = [e["event_type"] for e in events]
        self.assertIn("session_started", event_types)
        self.assertIn("signal_generated", event_types)
        self.assertIn("orchestrator_error", event_types)
        self.assertIn("session_failed", event_types)

    def test_secrets_in_extra_metadata_are_redacted(self) -> None:
        self.sink.start_session(
            make_metadata(
                "session-j",
                extra={
                    "api_key": "super-secret-key",
                    "access_token": "super-secret-token",
                    "base_url": "https://api.groww.example",
                    "safe_field": "this is fine",
                },
            )
        )

        raw_text = (self._session_dir("session-j") / "session.json").read_text()
        self.assertNotIn("super-secret-key", raw_text)
        self.assertNotIn("super-secret-token", raw_text)

        data = self._read_session_json("session-j")
        self.assertEqual(data["metadata"]["extra"]["api_key"], "***REDACTED***")
        self.assertEqual(data["metadata"]["extra"]["access_token"], "***REDACTED***")
        self.assertEqual(data["metadata"]["extra"]["safe_field"], "this is fine")
        # base_url isn't a credential itself and should pass through untouched.
        self.assertEqual(data["metadata"]["extra"]["base_url"], "https://api.groww.example")

    def test_multiple_sessions_are_isolated_in_separate_directories(self) -> None:
        sink1 = FileAuditSink(base_dir=self.tmp_dir)
        sink2 = FileAuditSink(base_dir=self.tmp_dir)

        sink1.start_session(make_metadata("session-k1"))
        sink2.start_session(make_metadata("session-k2"))

        sink1.record_event(AuditEvent(event_type=AuditEventType.SIGNAL_GENERATED, symbol="RELIANCE"))

        self.assertEqual(len(self._read_events("session-k1")), 2)  # started + signal
        self.assertEqual(len(self._read_events("session-k2")), 1)  # started only


class FindUncleanPriorSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="audit_test_"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_no_base_dir_returns_none(self) -> None:
        missing = self.tmp_dir / "does_not_exist"
        self.assertIsNone(find_unclean_prior_session(base_dir=missing))

    def test_no_prior_sessions_returns_none(self) -> None:
        self.assertIsNone(find_unclean_prior_session(base_dir=self.tmp_dir))

    def test_cleanly_completed_session_returns_none(self) -> None:
        sink = FileAuditSink(base_dir=self.tmp_dir)
        sink.start_session(make_metadata("session-clean"))
        sink.finalize_session(make_summary(succeeded=True))

        self.assertIsNone(find_unclean_prior_session(base_dir=self.tmp_dir))

    def test_cleanly_failed_session_returns_none(self) -> None:
        sink = FileAuditSink(base_dir=self.tmp_dir)
        sink.start_session(make_metadata("session-failed"))
        sink.record_failure(RuntimeError("handled failure"))

        self.assertIsNone(find_unclean_prior_session(base_dir=self.tmp_dir))

    def test_session_stuck_running_is_flagged(self) -> None:
        sink = FileAuditSink(base_dir=self.tmp_dir)
        sink.start_session(make_metadata("session-crashed"))
        # process "dies" here - never calls finalize_session/record_failure

        self.assertEqual(find_unclean_prior_session(base_dir=self.tmp_dir), "session-crashed")

    def test_only_the_most_recent_session_is_checked(self) -> None:
        sink1 = FileAuditSink(base_dir=self.tmp_dir)
        sink1.start_session(make_metadata("session-a-crashed"))  # older, crashed

        sink2 = FileAuditSink(base_dir=self.tmp_dir)
        sink2.start_session(make_metadata("session-b-clean"))
        sink2.finalize_session(make_summary(succeeded=True))  # newer, clean

        self.assertIsNone(find_unclean_prior_session(base_dir=self.tmp_dir))


if __name__ == "__main__":
    unittest.main()

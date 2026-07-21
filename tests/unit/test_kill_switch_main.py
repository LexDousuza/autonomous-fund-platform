"""
Unit tests for apps.live_trader.kill_switch_main.

Covers: the preview-only default (no --yes) never touches the broker's
place_order/cancel_order, the --yes path actually flattens and persists
an audit trail, credential handling for the groww provider, and the
CLI's exit code reflecting whether manual follow-up is needed.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_kill_switch_main.py -v
"""
from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from apps.live_trader.config import MissingGrowwCredentialsError
from apps.live_trader.kill_switch_main import main, parse_args, run_kill_switch
from core.audit.models import AuditEvent, AuditEventType, SessionMetadata, SessionSummary
from core.audit.sink import AuditSink
from core.notifications.alert_sink import NullAlertSink
from core.broker.base import OrderRequest, OrderSide, OrderType, ProductType
from core.broker.paper import PaperBroker

VALID_GROWW_ENV = {
    "GROWW_API_KEY": "test-api-key",
    "GROWW_ACCESS_TOKEN": "test-access-token",
}


class RecordingAuditSink(AuditSink):
    def __init__(self) -> None:
        self.started_with: SessionMetadata | None = None
        self.events: list[AuditEvent] = []
        self.finalized_with: SessionSummary | None = None

    def start_session(self, metadata: SessionMetadata) -> None:
        self.started_with = metadata

    def record_event(self, event: AuditEvent) -> None:
        self.events.append(event)

    def finalize_session(self, summary: SessionSummary) -> None:
        self.finalized_with = summary

    def record_failure(self, error: BaseException) -> None:
        pass


def _paper_broker_with_one_position() -> PaperBroker:
    broker = PaperBroker()
    broker.place_order(
        OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=10,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            price=2500.0,
        )
    )
    return broker


class RunKillSwitchPreviewTests(unittest.TestCase):
    def test_without_yes_never_places_or_cancels_anything(self) -> None:
        broker = _paper_broker_with_one_position()

        with patch("apps.live_trader.kill_switch_main.build_broker", return_value=broker):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                result = run_kill_switch("paper", confirmed=False)

        self.assertIsNone(result)
        self.assertIn("RELIANCE", buffer.getvalue())
        self.assertIn("Preview only", buffer.getvalue())
        # The one pre-existing position is untouched: still open, no new orders.
        positions = {p.symbol: p.quantity for p in broker.list_positions()}
        self.assertEqual(positions["RELIANCE"], 10)


class RunKillSwitchConfirmedTests(unittest.TestCase):
    def test_with_yes_flattens_positions_and_persists_audit_trail(self) -> None:
        broker = _paper_broker_with_one_position()
        sink = RecordingAuditSink()

        with patch("apps.live_trader.kill_switch_main.build_broker", return_value=broker):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                result = run_kill_switch("paper", confirmed=True, audit_sink=sink, alert_sink=NullAlertSink())

        self.assertIsNotNone(result)
        self.assertTrue(result.succeeded)
        self.assertEqual(len(result.flattened_positions), 1)
        self.assertEqual(broker.list_positions()[0].quantity, 0)

        self.assertIsNotNone(sink.started_with)
        self.assertEqual(sink.started_with.app_name, "kill_switch")
        self.assertIsNotNone(sink.finalized_with)
        self.assertTrue(sink.finalized_with.succeeded)
        event_types = [e.event_type for e in sink.events]
        self.assertIn(AuditEventType.KILL_SWITCH_TRIGGERED, event_types)
        self.assertIn(AuditEventType.KILL_SWITCH_COMPLETED, event_types)

    def test_no_open_positions_still_succeeds(self) -> None:
        broker = PaperBroker()
        sink = RecordingAuditSink()

        with patch("apps.live_trader.kill_switch_main.build_broker", return_value=broker):
            with redirect_stdout(io.StringIO()):
                result = run_kill_switch("paper", confirmed=True, audit_sink=sink, alert_sink=NullAlertSink())

        self.assertTrue(result.succeeded)
        self.assertEqual(result.flattened_positions, [])


class BuildBrokerCredentialsTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=False)
    def test_groww_without_credentials_raises(self) -> None:
        for var in ("GROWW_API_KEY", "GROWW_ACCESS_TOKEN"):
            os.environ.pop(var, None)

        with self.assertRaises(MissingGrowwCredentialsError):
            run_kill_switch("groww", confirmed=False)


class ParseArgsTests(unittest.TestCase):
    def test_defaults_to_paper_and_no_confirmation(self) -> None:
        args = parse_args([])
        self.assertEqual(args.broker, "paper")
        self.assertFalse(args.yes)
        self.assertEqual(args.order_ids, [])

    def test_parses_yes_and_repeated_order_ids(self) -> None:
        args = parse_args(
            ["--broker", "groww", "--yes", "--cancel-order-id", "a1", "--cancel-order-id", "b2"]
        )
        self.assertEqual(args.broker, "groww")
        self.assertTrue(args.yes)
        self.assertEqual(args.order_ids, ["a1", "b2"])


class MainExitCodeTests(unittest.TestCase):
    def test_preview_run_exits_zero(self) -> None:
        broker = PaperBroker()
        with patch("apps.live_trader.kill_switch_main.build_broker", return_value=broker):
            with redirect_stdout(io.StringIO()):
                exit_code = main(["--broker", "paper"])
        self.assertEqual(exit_code, 0)

    def test_confirmed_run_with_no_failures_exits_zero(self) -> None:
        broker = _paper_broker_with_one_position()
        with patch("apps.live_trader.kill_switch_main.build_broker", return_value=broker):
            with redirect_stdout(io.StringIO()):
                exit_code = main(["--broker", "paper", "--yes"])
        self.assertEqual(exit_code, 0)

    def test_confirmed_run_with_a_failure_exits_nonzero(self) -> None:
        broker = _paper_broker_with_one_position()

        with patch("apps.live_trader.kill_switch_main.build_broker", return_value=broker):
            with patch.object(
                broker, "place_order", side_effect=RuntimeError("broker rejected order")
            ):
                with redirect_stdout(io.StringIO()):
                    exit_code = main(["--broker", "paper", "--yes"])

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for core.notifications.alert_sink."""
from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from core.notifications.alert_sink import ConsoleAlertSink, NullAlertSink


class NullAlertSinkTests(unittest.TestCase):
    def test_send_is_a_no_op(self) -> None:
        NullAlertSink().send("anything")  # must not raise


class ConsoleAlertSinkTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.log_path = Path(tmp_dir.name) / "alerts.log"
        self.sink = ConsoleAlertSink(log_path=self.log_path)

    def test_appends_message_to_log_file(self) -> None:
        self.sink.send("session died", severity="critical")

        content = self.log_path.read_text()
        self.assertIn("CRITICAL", content)
        self.assertIn("session died", content)

    def test_multiple_sends_append_rather_than_overwrite(self) -> None:
        self.sink.send("first")
        self.sink.send("second")

        lines = self.log_path.read_text().splitlines()
        self.assertEqual(len(lines), 2)

    def test_no_webhook_configured_does_not_raise(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("ALERT_WEBHOOK_URL", None)
            self.sink.send("no webhook set")  # must not raise

    def test_webhook_failure_is_swallowed_not_raised(self) -> None:
        with patch.dict("os.environ", {"ALERT_WEBHOOK_URL": "https://example.invalid/hook"}):
            with patch(
                "urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")
            ):
                self.sink.send("webhook is down")  # must not raise despite failure

    def test_webhook_is_called_when_configured(self) -> None:
        with patch.dict("os.environ", {"ALERT_WEBHOOK_URL": "https://example.invalid/hook"}):
            with patch("urllib.request.urlopen") as mock_urlopen:
                self.sink.send("hello")
                mock_urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()

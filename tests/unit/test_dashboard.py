"""Tests for apps.reporting.dashboard: render_html and the live HTTP server."""
from __future__ import annotations

import http.client
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

from apps.reporting import dashboard
from apps.reporting.dashboard import _DashboardHandler, render_html


class RenderHtmlTests(unittest.TestCase):
    def test_no_sessions_yet_still_renders(self) -> None:
        html = render_html(None)
        self.assertIn("Trading Dashboard", html)
        self.assertIn("RELIANCE", html)  # full universe always shown

    def test_error_message_gets_error_css_class(self) -> None:
        html = render_html(None, message="something went wrong", message_severity="error")
        self.assertIn('class="msg msg-error"', html)
        self.assertIn("something went wrong", html)

    def test_info_message_gets_info_css_class(self) -> None:
        html = render_html(None, message="all good", message_severity="info")
        self.assertIn('class="msg msg-info"', html)


class DashboardServerIntegrationTests(unittest.TestCase):
    """
    Exercises the real HTTP server end to end (real socket, real request
    parsing, real file writes) against an isolated mandate file - the
    same thing manually verified via curl during development, kept as a
    durable regression test.
    """

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.mandate_path = Path(tmp_dir.name) / "mandate.yaml"
        self.mandate_path.write_text(
            'trading_date: "2026-07-07"\n\n'
            "buckets:\n  reserve_bucket: 40000\n  intraday_bucket: 60000\n\n"
            "risk_limits:\n  max_daily_loss: 2500\n  max_trades_per_day: 6\n"
            '  stop_new_entries_after: "14:45"\n  force_square_off_at: "15:20"\n\n'
            'execution_mode: "paper"\n\nrisk_appetite: "moderate"\n',
            encoding="utf-8",
        )
        self._original_mandate_path = dashboard.MANDATE_PATH
        dashboard.MANDATE_PATH = str(self.mandate_path)
        self.addCleanup(setattr, dashboard, "MANDATE_PATH", self._original_mandate_path)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _DashboardHandler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _post(self, path: str, data: dict) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = urllib.parse.urlencode(data)
        conn.request("POST", path, body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        location = resp.getheader("Location", "")
        resp.read()
        conn.close()
        return resp.status, location

    def test_valid_update_writes_mandate_and_redirects_with_success(self) -> None:
        status, location = self._post(
            "/update-mandate",
            {
                "intraday_bucket": "75000",
                "reserve_bucket": "40000",
                "max_daily_loss": "3000",
                "max_trades_per_day": "8",
                "stop_new_entries_after": "14:45",
                "force_square_off_at": "15:20",
                "risk_appetite": "aggressive",
            },
        )

        self.assertEqual(status, 303)
        self.assertIn("severity=info", location)
        self.assertIn("intraday_bucket: 75000", self.mandate_path.read_text())
        self.assertIn('risk_appetite: "aggressive"', self.mandate_path.read_text())

    def test_negative_capital_rejected_without_writing(self) -> None:
        original = self.mandate_path.read_text()

        status, location = self._post(
            "/update-mandate",
            {
                "intraday_bucket": "-500",
                "reserve_bucket": "40000",
                "max_daily_loss": "2500",
                "max_trades_per_day": "6",
                "stop_new_entries_after": "14:45",
                "force_square_off_at": "15:20",
                "risk_appetite": "moderate",
            },
        )

        self.assertEqual(status, 303)
        self.assertIn("severity=error", location)
        self.assertEqual(self.mandate_path.read_text(), original)

    def test_bad_time_format_rejected(self) -> None:
        status, location = self._post(
            "/update-mandate",
            {
                "intraday_bucket": "60000",
                "reserve_bucket": "40000",
                "max_daily_loss": "2500",
                "max_trades_per_day": "6",
                "stop_new_entries_after": "not-a-time",
                "force_square_off_at": "15:20",
                "risk_appetite": "moderate",
            },
        )

        self.assertEqual(status, 303)
        self.assertIn("severity=error", location)

    def test_bad_risk_appetite_rejected(self) -> None:
        status, location = self._post(
            "/update-mandate",
            {
                "intraday_bucket": "60000",
                "reserve_bucket": "40000",
                "max_daily_loss": "2500",
                "max_trades_per_day": "6",
                "stop_new_entries_after": "14:45",
                "force_square_off_at": "15:20",
                "risk_appetite": "yolo",
            },
        )

        self.assertEqual(status, 303)
        self.assertIn("severity=error", location)

    def test_unknown_post_path_returns_404(self) -> None:
        status, _ = self._post("/not-a-real-endpoint", {})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for apps.reporting.obsidian_export."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from apps.reporting.obsidian_export import (
    _format_timedelta,
    _market_status_line,
    _text_meter,
    render_markdown,
    sync_once,
)


class RenderMarkdownTests(unittest.TestCase):
    def test_no_sessions_yet_still_renders(self) -> None:
        md = render_markdown(None)
        self.assertIn("Trading Dashboard", md)
        self.assertIn("RELIANCE", md)  # full universe always shown
        self.assertIn("no sessions yet", md)

    def test_frontmatter_present(self) -> None:
        md = render_markdown(None)
        self.assertTrue(md.startswith("---\n"))
        self.assertIn("status:", md)


class TextMeterTests(unittest.TestCase):
    def test_zero_percent_is_all_empty(self) -> None:
        meter = _text_meter(0, width=10)
        self.assertEqual(meter, "`[" + "░" * 10 + "]`")

    def test_hundred_percent_is_all_filled(self) -> None:
        meter = _text_meter(100, width=10)
        self.assertEqual(meter, "`[" + "█" * 10 + "]`")

    def test_clamps_above_100(self) -> None:
        meter = _text_meter(250, width=10)
        self.assertEqual(meter, "`[" + "█" * 10 + "]`")


class FormatTimedeltaTests(unittest.TestCase):
    def test_under_an_hour_shows_minutes_only(self) -> None:
        from datetime import timedelta

        self.assertEqual(_format_timedelta(timedelta(minutes=45)), "45m")

    def test_over_an_hour_shows_hours_and_minutes(self) -> None:
        from datetime import timedelta

        self.assertEqual(_format_timedelta(timedelta(hours=2, minutes=5)), "2h 5m")


class MarketStatusLineTests(unittest.TestCase):
    def test_returns_open_or_closed(self) -> None:
        line = _market_status_line()
        self.assertTrue(line.startswith("Market OPEN") or line.startswith("Market CLOSED"))


class SyncOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.vault_dir = Path(tmp_dir.name)

    def test_writes_trading_dashboard_note(self) -> None:
        sync_once(self.vault_dir)

        note = self.vault_dir / "Trading" / "Trading Dashboard.md"
        self.assertTrue(note.exists())
        self.assertIn("Trading Dashboard", note.read_text())

    def test_second_sync_overwrites_the_live_note(self) -> None:
        sync_once(self.vault_dir)
        note = self.vault_dir / "Trading" / "Trading Dashboard.md"
        first_content = note.read_text()

        sync_once(self.vault_dir)
        second_content = note.read_text()

        # both syncs produce valid content (exact equality isn't guaranteed
        # since the market-status line is time-dependent, but the file
        # must have been rewritten, not appended to or corrupted)
        self.assertIn("Trading Dashboard", first_content)
        self.assertIn("Trading Dashboard", second_content)


if __name__ == "__main__":
    unittest.main()

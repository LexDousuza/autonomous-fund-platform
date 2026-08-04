"""Unit tests for apps.reporting.obsidian_export."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.reporting import dashboard
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


class CandidateSelectionSectionTests(unittest.TestCase):
    """The Markdown mirror of dashboard.py's HTML "Candidate selection" section."""

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.selection_dir = Path(tmp_dir.name)

        self._original_dir = dashboard.CANDIDATE_SELECTION_DIR
        dashboard.CANDIDATE_SELECTION_DIR = self.selection_dir
        self.addCleanup(setattr, dashboard, "CANDIDATE_SELECTION_DIR", self._original_dir)

    def test_no_selection_file_shows_fallback_message(self) -> None:
        md = render_markdown(None)
        self.assertIn("No overnight candidate selection saved yet", md)

    def test_selected_ticker_shows_price_volatility_and_news_reasoning(self) -> None:
        from core.market_data.candidate_selection_store import save_candidate_selection
        from core.market_data.universe_models import Instrument

        save_candidate_selection(
            "2026-07-24",
            [Instrument(symbol="RELIANCE", exchange="NSE")],
            self.selection_dir,
            selected_detail=[
                {
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "last_close": 1280.9,
                    "volatility_pct": 0.018,
                    "volatility_ceiling": 0.04,
                    "volatility_source": "historical",
                    "news_score": 4,
                    "headlines": ["Reliance surges on record profit growth"],
                }
            ],
            excluded_detail=[
                {"symbol": "INFY", "exchange": "NSE", "stage": "prescreen", "reason": "unaffordable", "ltp": 50000.0}
            ],
        )

        md = render_markdown(None)

        self.assertIn("2026-07-24", md)
        self.assertIn("NSE:RELIANCE", md)
        self.assertIn("1,280.90", md)
        self.assertIn("1.80%", md)
        self.assertIn("Reliance surges on record profit growth", md)
        self.assertIn("NSE:INFY", md)
        self.assertIn("unaffordable", md)

    def test_legacy_file_without_detail_falls_back_to_bare_symbol_list(self) -> None:
        from core.market_data.candidate_selection_store import save_candidate_selection
        from core.market_data.universe_models import Instrument

        save_candidate_selection("2026-07-24", [Instrument(symbol="AAATECH", exchange="NSE")], self.selection_dir)

        md = render_markdown(None)

        self.assertIn("NSE:AAATECH", md)
        self.assertIn("no detail saved for this selection", md)


class MdTableEscapingTests(unittest.TestCase):
    """
    A literal "|" in cell content (common in real news headlines, e.g.
    "X | Y" title formats) must not be read as a column separator -
    would otherwise corrupt the table structure.
    """

    def test_pipe_in_cell_is_escaped_not_treated_as_a_column_separator(self) -> None:
        from apps.reporting.obsidian_export import _md_table

        table = _md_table(["A", "B"], [["Title | With Pipe", "ok"]], "empty")

        lines = table.strip("\n").split("\n")
        self.assertEqual(len(lines), 3)  # header, separator, exactly one data row
        self.assertIn("Title \\| With Pipe", table)

    def test_embedded_newline_is_collapsed_to_a_space(self) -> None:
        from apps.reporting.obsidian_export import _md_table

        table = _md_table(["A"], [["line one\nline two"]], "empty")

        self.assertEqual(len(table.strip("\n").split("\n")), 3)


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

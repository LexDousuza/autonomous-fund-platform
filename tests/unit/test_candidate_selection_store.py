"""
Unit tests for core.market_data.candidate_selection_store.

Every test uses its own tempfile.TemporaryDirectory() as the store
directory - never touches the real data/candidate_selection/.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_candidate_selection_store.py -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.market_data.candidate_selection_store import (
    load_candidate_selection,
    save_candidate_selection,
)
from core.market_data.universe_models import Instrument


class CandidateSelectionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.directory = Path(self._tmp_dir.name)

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(load_candidate_selection("2026-07-24", self.directory))

    def test_strategy_suffixed_and_legacy_files_are_independent(self) -> None:
        legacy = [Instrument(symbol="RELIANCE", exchange="NSE")]
        momentum = [Instrument(symbol="TCS", exchange="NSE")]

        save_candidate_selection("2026-07-24", legacy, self.directory)
        save_candidate_selection("2026-07-24", momentum, self.directory, strategy_name="momentum")

        self.assertTrue((self.directory / "2026-07-24.json").exists())
        self.assertTrue((self.directory / "2026-07-24_momentum.json").exists())
        self.assertEqual(
            [i.symbol for i in load_candidate_selection("2026-07-24", self.directory).instruments],
            ["RELIANCE"],
        )
        self.assertEqual(
            [
                i.symbol
                for i in load_candidate_selection(
                    "2026-07-24", self.directory, strategy_name="momentum"
                ).instruments
            ],
            ["TCS"],
        )

    def test_strategy_name_with_nothing_saved_is_none_not_a_fallback(self) -> None:
        save_candidate_selection("2026-07-24", [Instrument(symbol="RELIANCE", exchange="NSE")], self.directory)

        self.assertIsNone(
            load_candidate_selection("2026-07-24", self.directory, strategy_name="pullback")
        )

    def test_round_trips_symbols_and_exchange(self) -> None:
        instruments = [
            Instrument(symbol="RELIANCE", exchange="NSE"),
            Instrument(symbol="TCS", exchange="NSE"),
        ]

        save_candidate_selection("2026-07-24", instruments, self.directory)
        loaded = load_candidate_selection("2026-07-24", self.directory)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.trading_date, "2026-07-24")
        self.assertEqual([(i.symbol, i.exchange) for i in loaded.instruments], [("RELIANCE", "NSE"), ("TCS", "NSE")])

    def test_selected_and_excluded_detail_round_trip(self) -> None:
        selected_detail = [
            {"symbol": "RELIANCE", "exchange": "NSE", "last_close": 1280.9, "news_score": 4, "headlines": ["Reliance surges on record profit"]}
        ]
        excluded_detail = [
            {"symbol": "TCS", "exchange": "NSE", "stage": "prescreen", "reason": "unaffordable", "ltp": 3550.0}
        ]

        save_candidate_selection(
            "2026-07-24",
            [Instrument(symbol="RELIANCE", exchange="NSE")],
            self.directory,
            selected_detail=selected_detail,
            excluded_detail=excluded_detail,
        )
        loaded = load_candidate_selection("2026-07-24", self.directory)

        self.assertEqual(loaded.selected_detail, selected_detail)
        self.assertEqual(loaded.excluded_detail, excluded_detail)

    def test_detail_defaults_to_empty_lists_when_not_provided(self) -> None:
        save_candidate_selection("2026-07-24", [Instrument(symbol="RELIANCE", exchange="NSE")], self.directory)

        loaded = load_candidate_selection("2026-07-24", self.directory)

        self.assertEqual(loaded.selected_detail, [])
        self.assertEqual(loaded.excluded_detail, [])

    def test_different_trading_dates_are_independent_files(self) -> None:
        save_candidate_selection("2026-07-24", [Instrument(symbol="RELIANCE", exchange="NSE")], self.directory)

        self.assertIsNone(load_candidate_selection("2026-07-25", self.directory))

    def test_saving_again_for_same_date_overwrites_not_appends(self) -> None:
        save_candidate_selection("2026-07-24", [Instrument(symbol="RELIANCE", exchange="NSE")], self.directory)
        save_candidate_selection("2026-07-24", [Instrument(symbol="TCS", exchange="NSE")], self.directory)

        loaded = load_candidate_selection("2026-07-24", self.directory)

        self.assertEqual([(i.symbol, i.exchange) for i in loaded.instruments], [("TCS", "NSE")])

    def test_empty_selection_round_trips_as_empty_list(self) -> None:
        save_candidate_selection("2026-07-24", [], self.directory)

        loaded = load_candidate_selection("2026-07-24", self.directory)

        self.assertEqual(loaded.instruments, [])

    def test_malformed_file_raises_rather_than_silently_returning_none(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "2026-07-24.json").write_text("not valid json", encoding="utf-8")

        with self.assertRaises(json.JSONDecodeError):
            load_candidate_selection("2026-07-24", self.directory)


if __name__ == "__main__":
    unittest.main()

"""
Unit tests for core.market_data universe loading and selection.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_universe.py -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.market_data.universe_loader import (
    add_instrument_to_universe,
    load_instrument_universe,
    write_instrument_universe,
)
from core.market_data.universe_models import Instrument, InstrumentUniverse
from core.market_data.universe_selector import (
    TradingUniverseSelector,
    UniverseSelectionCriteria,
    select_session_instruments,
)

UNIVERSE_PATH = "config/instruments/india_cash_universe.yaml"


def make_universe() -> InstrumentUniverse:
    return InstrumentUniverse(
        universe_name="test_universe",
        instruments=[
            Instrument(symbol="AAA", exchange="NSE", segment="CASH", enabled=True),
            Instrument(symbol="BBB", exchange="NSE", segment="CASH", enabled=False),
            Instrument(symbol="CCC", exchange="BSE", segment="CASH", enabled=True),
            Instrument(symbol="DDD", exchange="NSE", segment="DERIVATIVES", enabled=True),
            Instrument(
                symbol="EEE", exchange="NSE", segment="CASH", enabled=True, tags=["banking"]
            ),
        ],
    )


class InstrumentUniverseLoaderTests(unittest.TestCase):
    def test_loads_universe_name_and_instrument_count(self) -> None:
        universe = load_instrument_universe(UNIVERSE_PATH)

        self.assertEqual(universe.universe_name, "india_cash_v1")
        self.assertEqual(len(universe.instruments), 2395)

    def test_first_instrument_fields_are_parsed(self) -> None:
        universe = load_instrument_universe(UNIVERSE_PATH)
        first = universe.instruments[0]

        self.assertEqual(first.symbol, "20MICRONS")
        self.assertEqual(first.exchange, "NSE")
        self.assertEqual(first.segment, "CASH")
        self.assertTrue(first.enabled)

    def test_all_configured_instruments_are_enabled(self) -> None:
        universe = load_instrument_universe(UNIVERSE_PATH)

        self.assertEqual(len(universe.enabled_instruments()), len(universe.instruments))

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_instrument_universe("config/instruments/does_not_exist.yaml")

    def test_unknown_fields_preserved_in_metadata(self) -> None:
        universe = InstrumentUniverse(
            universe_name="x",
            instruments=[
                Instrument(symbol="AAA", exchange="NSE", metadata={"lot_size": 1}),
            ],
        )
        self.assertEqual(universe.instruments[0].metadata, {"lot_size": 1})


class AddInstrumentToUniverseTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.path = Path(tmp_dir.name) / "universe.yaml"
        self.path.write_text(
            "universe_name: test\ninstruments:\n"
            "  - symbol: AAA\n    exchange: NSE\n    segment: CASH\n    enabled: true\n"
        )

    def test_appends_new_instrument(self) -> None:
        universe = add_instrument_to_universe(self.path, "wipro", "nse", "CASH")

        self.assertEqual([i.symbol for i in universe.instruments], ["AAA", "WIPRO"])
        self.assertEqual(universe.instruments[-1].exchange, "NSE")
        self.assertTrue(universe.instruments[-1].enabled)

    def test_persists_to_disk(self) -> None:
        add_instrument_to_universe(self.path, "WIPRO", "NSE")

        reloaded = load_instrument_universe(self.path)
        self.assertEqual([i.symbol for i in reloaded.instruments], ["AAA", "WIPRO"])

    def test_duplicate_symbol_exchange_is_a_no_op(self) -> None:
        add_instrument_to_universe(self.path, "AAA", "NSE")
        universe = add_instrument_to_universe(self.path, "AAA", "NSE")

        self.assertEqual(len(universe.instruments), 1)


class WriteInstrumentUniverseTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.path = Path(tmp_dir.name) / "universe.yaml"

    def test_round_trips_through_load(self) -> None:
        universe = InstrumentUniverse(
            universe_name="screened_v1",
            instruments=[
                Instrument(symbol="AAA", exchange="NSE", segment="CASH", enabled=True),
                Instrument(symbol="BBB", exchange="NSE", segment="CASH", enabled=False, tags=["it"]),
            ],
        )

        write_instrument_universe(self.path, universe)
        reloaded = load_instrument_universe(self.path)

        self.assertEqual(reloaded.universe_name, "screened_v1")
        self.assertEqual([i.symbol for i in reloaded.instruments], ["AAA", "BBB"])
        self.assertFalse(reloaded.instruments[1].enabled)
        self.assertEqual(reloaded.instruments[1].tags, ["it"])

    def test_overwrites_an_existing_file_entirely(self) -> None:
        self.path.write_text(
            "universe_name: old\ninstruments:\n"
            "  - symbol: ZZZ\n    exchange: NSE\n    segment: CASH\n    enabled: true\n"
        )

        write_instrument_universe(
            self.path,
            InstrumentUniverse(
                universe_name="new",
                instruments=[Instrument(symbol="AAA", exchange="NSE", segment="CASH")],
            ),
        )
        reloaded = load_instrument_universe(self.path)

        self.assertEqual(reloaded.universe_name, "new")
        self.assertEqual([i.symbol for i in reloaded.instruments], ["AAA"])


class TradingUniverseSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.universe = make_universe()
        self.selector = TradingUniverseSelector()

    def test_no_criteria_returns_all_enabled_in_source_order(self) -> None:
        selected = self.selector.select(self.universe)

        self.assertEqual([i.symbol for i in selected], ["AAA", "CCC", "DDD", "EEE"])

    def test_disabled_instruments_are_excluded(self) -> None:
        selected = self.selector.select(self.universe)

        self.assertNotIn("BBB", [i.symbol for i in selected])

    def test_filters_by_exchange(self) -> None:
        selected = self.selector.select(
            self.universe, UniverseSelectionCriteria(exchange="NSE")
        )

        self.assertEqual([i.symbol for i in selected], ["AAA", "DDD", "EEE"])

    def test_filters_by_segment(self) -> None:
        selected = self.selector.select(
            self.universe, UniverseSelectionCriteria(segment="CASH")
        )

        self.assertEqual([i.symbol for i in selected], ["AAA", "CCC", "EEE"])

    def test_filters_by_tags(self) -> None:
        selected = self.selector.select(
            self.universe, UniverseSelectionCriteria(tags=["banking"])
        )

        self.assertEqual([i.symbol for i in selected], ["EEE"])

    def test_caps_max_symbols(self) -> None:
        selected = self.selector.select(self.universe, UniverseSelectionCriteria(max_symbols=2))

        self.assertEqual([i.symbol for i in selected], ["AAA", "CCC"])

    def test_combined_filters_preserve_deterministic_order(self) -> None:
        selected = self.selector.select(
            self.universe,
            UniverseSelectionCriteria(exchange="NSE", segment="CASH", max_symbols=1),
        )

        self.assertEqual([i.symbol for i in selected], ["AAA"])

    def test_selection_is_repeatable(self) -> None:
        criteria = UniverseSelectionCriteria(exchange="NSE")
        first = self.selector.select(self.universe, criteria)
        second = self.selector.select(self.universe, criteria)

        self.assertEqual([i.symbol for i in first], [i.symbol for i in second])

    def test_convenience_function_matches_selector(self) -> None:
        criteria = UniverseSelectionCriteria(segment="CASH")

        via_function = select_session_instruments(self.universe, criteria)
        via_selector = self.selector.select(self.universe, criteria)

        self.assertEqual(via_function, via_selector)


INDEX_UNIVERSE_PATH = "config/instruments/india_index_universe.yaml"


class IndexInstrumentUniverseTests(unittest.TestCase):
    """
    NIFTY/BANKNIFTY as plain Instrument rows in their own universe file -
    see CandidateProfile.fixed_universe_segment's docstring for why
    they're not in india_cash_universe.yaml.
    """

    def test_loads_both_index_instruments(self) -> None:
        universe = load_instrument_universe(INDEX_UNIVERSE_PATH)

        self.assertEqual(universe.universe_name, "india_index_v1")
        self.assertEqual({i.symbol for i in universe.instruments}, {"NIFTY", "BANKNIFTY"})

    def test_both_instruments_are_index_segment_and_tagged(self) -> None:
        universe = load_instrument_universe(INDEX_UNIVERSE_PATH)

        for instrument in universe.instruments:
            self.assertEqual(instrument.exchange, "NSE")
            self.assertEqual(instrument.segment, "INDEX")
            self.assertIn("index_options", instrument.tags)
            self.assertTrue(instrument.enabled)

    def test_selectable_by_index_segment(self) -> None:
        universe = load_instrument_universe(INDEX_UNIVERSE_PATH)

        selected = TradingUniverseSelector().select(universe, UniverseSelectionCriteria(segment="INDEX"))

        self.assertEqual({i.symbol for i in selected}, {"NIFTY", "BANKNIFTY"})


if __name__ == "__main__":
    unittest.main()

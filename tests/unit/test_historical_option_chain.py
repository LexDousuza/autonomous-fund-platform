"""
Unit tests for core.market_data.historical_option_chain.

Every test writes its own small fixture CSV to a tempfile.TemporaryDirectory()
- never touches the real, ~600MB data/processed/nse_index_options_iv_history.csv.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_historical_option_chain.py -v
"""
from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from core.broker.base import OptionType
from core.market_data.historical_option_chain import HistoricalOptionChainProvider

_HEADER = [
    "trade_date", "symbol", "expiry", "strike", "option_type", "settle_price",
    "spot", "time_to_expiry_years", "risk_free_rate", "implied_vol", "open_interest", "volume",
]


def _row(trade_date, symbol, expiry, strike, option_type, settle_price, spot, iv, oi=1000, volume=500):
    return [trade_date, symbol, expiry, strike, option_type, settle_price, spot, "", 0.07, iv, oi, volume]


def _write_fixture_csv(directory: Path, rows: list[list]) -> Path:
    path = directory / "fixture_iv_history.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_HEADER)
        writer.writerows(rows)
    return path


_DAY1 = "2026-01-05"
_DAY2 = "2026-01-06"
_NEAR_EXPIRY = "2026-01-08"  # 3 days out from DAY1, 2 days out from DAY2
_FAR_EXPIRY = "2026-02-26"  # far beyond any horizon used below


class HistoricalOptionChainProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.directory = Path(self._tmp_dir.name)

    def test_get_chain_before_set_current_date_raises(self) -> None:
        csv_path = _write_fixture_csv(self.directory, [])
        provider = HistoricalOptionChainProvider(csv_path=csv_path)

        with self.assertRaises(RuntimeError):
            provider.get_chain("NIFTY", "NSE", expiry_horizon_days=7, spot=20000.0)

    def test_returns_snapshot_with_real_contracts_for_current_date(self) -> None:
        rows = [
            _row(_DAY1, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 120.5, 20000.0, 0.14),
            _row(_DAY1, "NIFTY", _NEAR_EXPIRY, 19600.0, "PE", 150.0, 20000.0, 0.145),
            _row(_DAY1, "NIFTY", _NEAR_EXPIRY, 20500.0, "CE", 90.0, 20000.0, 0.13),
        ]
        csv_path = _write_fixture_csv(self.directory, rows)
        provider = HistoricalOptionChainProvider(csv_path=csv_path)
        provider.set_current_date(date(2026, 1, 5))

        snapshot = provider.get_chain("NIFTY", "NSE", expiry_horizon_days=7, spot=20000.0)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.expiry, _NEAR_EXPIRY)
        self.assertEqual(snapshot.underlying_last_price, 20000.0)
        self.assertEqual(len(snapshot.contracts), 3)
        by_strike = {c.strike: c for c in snapshot.contracts}
        self.assertEqual(by_strike[19500.0].option_type, OptionType.PUT)
        self.assertEqual(by_strike[19500.0].last_price, 120.5)
        self.assertAlmostEqual(by_strike[19500.0].iv, 0.14)
        self.assertEqual(by_strike[20500.0].option_type, OptionType.CALL)

    def test_contract_price_is_stable_identity_across_different_trade_dates(self) -> None:
        # Same contract (same expiry/strike/option_type), two different
        # trade dates, two different settle prices - proves a carried
        # position's own tradingsymbol resolves consistently day to day,
        # the exact property core.portfolio.position_manager relies on
        # to keep tracking the "same" leg across a multi-day hold.
        rows = [
            _row(_DAY1, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 120.5, 20000.0, 0.14),
            _row(_DAY2, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 95.0, 20100.0, 0.135),
        ]
        csv_path = _write_fixture_csv(self.directory, rows)
        provider = HistoricalOptionChainProvider(csv_path=csv_path)

        provider.set_current_date(date(2026, 1, 5))
        day1_snapshot = provider.get_chain("NIFTY", "NSE", expiry_horizon_days=7, spot=20000.0)
        day1_tradingsymbol = day1_snapshot.contracts[0].tradingsymbol

        provider.set_current_date(date(2026, 1, 6))
        day2_snapshot = provider.get_chain("NIFTY", "NSE", expiry_horizon_days=7, spot=20100.0)
        day2_tradingsymbol = day2_snapshot.contracts[0].tradingsymbol

        self.assertEqual(day1_tradingsymbol, day2_tradingsymbol)
        self.assertEqual(day1_snapshot.contracts[0].last_price, 120.5)
        self.assertEqual(day2_snapshot.contracts[0].last_price, 95.0)

    def test_get_contract_price_looks_up_a_specific_leg_on_a_later_date(self) -> None:
        # The exact lookup a multi-day day-loop needs to build a synthetic
        # exit-evaluation bar for a leg opened on an earlier day.
        rows = [
            _row(_DAY1, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 120.5, 20000.0, 0.14),
            _row(_DAY2, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 95.0, 20100.0, 0.135),
        ]
        csv_path = _write_fixture_csv(self.directory, rows)
        provider = HistoricalOptionChainProvider(csv_path=csv_path)

        price = provider.get_contract_price(
            "NIFTY", _NEAR_EXPIRY, 19500.0, OptionType.PUT, as_of=date(2026, 1, 6)
        )

        self.assertEqual(price, 95.0)

    def test_get_price_by_tradingsymbol_freezes_at_last_known_price_after_expiry(self) -> None:
        # A held position's own contract stops appearing in the source
        # data once it expires - real settlement data doesn't extend
        # past a contract's own last trade date. Without a fallback, a
        # multi-day runner asking for this contract's price on a LATER
        # calendar date (e.g. evaluating a still-open leg's exit) would
        # get None forever and the position would never be exit-
        # evaluated again - silently stuck open for the rest of the
        # backtest. "Frozen at its own last real settle price" is a
        # deliberate simplification (real index options cash-settle to
        # intrinsic value at expiry, not a frozen premium), documented
        # on the method itself - what matters here is that a later date
        # returns *something real*, not that it's the economically
        # perfect settlement number.
        rows = [
            _row(_DAY1, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 120.5, 20000.0, 0.14),
            _row(_NEAR_EXPIRY, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 40.0, 19600.0, 0.10),
        ]
        csv_path = _write_fixture_csv(self.directory, rows)
        provider = HistoricalOptionChainProvider(csv_path=csv_path)
        provider.set_current_date(date(2026, 1, 5))
        tradingsymbol = provider.get_chain("NIFTY", "NSE", 7, 20000.0).contracts[0].tradingsymbol

        after_expiry = date(2026, 1, 20)  # well past _NEAR_EXPIRY (2026-01-08), no CSV row at all
        price = provider.get_price_by_tradingsymbol(tradingsymbol, as_of=after_expiry)

        self.assertEqual(price, 40.0)  # the contract's own last known (expiry-day) price

    def test_get_contract_price_also_freezes_at_last_known_price_after_expiry(self) -> None:
        rows = [
            _row(_DAY1, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 120.5, 20000.0, 0.14),
            _row(_NEAR_EXPIRY, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 40.0, 19600.0, 0.10),
        ]
        csv_path = _write_fixture_csv(self.directory, rows)
        provider = HistoricalOptionChainProvider(csv_path=csv_path)

        price = provider.get_contract_price(
            "NIFTY", _NEAR_EXPIRY, 19500.0, OptionType.PUT, as_of=date(2026, 1, 20)
        )

        self.assertEqual(price, 40.0)

    def test_get_contract_price_unknown_contract_returns_none(self) -> None:
        csv_path = _write_fixture_csv(
            self.directory, [_row(_DAY1, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 120.5, 20000.0, 0.14)]
        )
        provider = HistoricalOptionChainProvider(csv_path=csv_path)

        price = provider.get_contract_price(
            "NIFTY", _NEAR_EXPIRY, 99999.0, OptionType.PUT, as_of=date(2026, 1, 5)
        )

        self.assertIsNone(price)

    def test_expiry_beyond_horizon_is_skipped(self) -> None:
        rows = [_row(_DAY1, "NIFTY", _FAR_EXPIRY, 19500.0, "PE", 500.0, 20000.0, 0.15)]
        csv_path = _write_fixture_csv(self.directory, rows)
        provider = HistoricalOptionChainProvider(csv_path=csv_path)
        provider.set_current_date(date(2026, 1, 5))

        snapshot = provider.get_chain("NIFTY", "NSE", expiry_horizon_days=7, spot=20000.0)

        self.assertIsNone(snapshot)

    def test_picks_nearest_expiry_within_horizon_not_furthest(self) -> None:
        rows = [
            _row(_DAY1, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 120.5, 20000.0, 0.14),
            _row(_DAY1, "NIFTY", _FAR_EXPIRY, 19500.0, "PE", 500.0, 20000.0, 0.15),
        ]
        csv_path = _write_fixture_csv(self.directory, rows)
        provider = HistoricalOptionChainProvider(csv_path=csv_path)
        provider.set_current_date(date(2026, 1, 5))

        snapshot = provider.get_chain("NIFTY", "NSE", expiry_horizon_days=60, spot=20000.0)

        self.assertEqual(snapshot.expiry, _NEAR_EXPIRY)

    def test_unknown_date_returns_none_not_a_crash(self) -> None:
        rows = [_row(_DAY1, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 120.5, 20000.0, 0.14)]
        csv_path = _write_fixture_csv(self.directory, rows)
        provider = HistoricalOptionChainProvider(csv_path=csv_path)
        provider.set_current_date(date(2026, 3, 1))  # no data for this date at all

        snapshot = provider.get_chain("NIFTY", "NSE", expiry_horizon_days=7, spot=20000.0)

        self.assertIsNone(snapshot)

    def test_get_price_by_tradingsymbol_tracks_a_carried_leg_across_days(self) -> None:
        rows = [
            _row(_DAY1, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 120.5, 20000.0, 0.14),
            _row(_DAY2, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 95.0, 20100.0, 0.135),
        ]
        csv_path = _write_fixture_csv(self.directory, rows)
        provider = HistoricalOptionChainProvider(csv_path=csv_path)
        provider.set_current_date(date(2026, 1, 5))
        tradingsymbol = provider.get_chain("NIFTY", "NSE", 7, 20000.0).contracts[0].tradingsymbol

        day1_price = provider.get_price_by_tradingsymbol(tradingsymbol, as_of=date(2026, 1, 5))
        day2_price = provider.get_price_by_tradingsymbol(tradingsymbol, as_of=date(2026, 1, 6))
        # A date past this contract's last real row - frozen at day 2's
        # price rather than None, see get_price_by_tradingsymbol's own
        # docstring (test_get_price_by_tradingsymbol_freezes_at_last_known_price_after_expiry
        # covers this fallback directly; this assertion is just proving
        # it doesn't undermine the two exact-match cases above).
        later_price = provider.get_price_by_tradingsymbol(tradingsymbol, as_of=date(2026, 3, 1))

        self.assertEqual(day1_price, 120.5)
        self.assertEqual(day2_price, 95.0)
        self.assertEqual(later_price, 95.0)

    def test_scoped_to_requested_underlying_symbol(self) -> None:
        rows = [
            _row(_DAY1, "NIFTY", _NEAR_EXPIRY, 19500.0, "PE", 120.5, 20000.0, 0.14),
            _row(_DAY1, "BANKNIFTY", _NEAR_EXPIRY, 45000.0, "PE", 400.0, 46000.0, 0.20),
        ]
        csv_path = _write_fixture_csv(self.directory, rows)
        provider = HistoricalOptionChainProvider(csv_path=csv_path)
        provider.set_current_date(date(2026, 1, 5))

        nifty_snapshot = provider.get_chain("NIFTY", "NSE", expiry_horizon_days=7, spot=20000.0)
        banknifty_snapshot = provider.get_chain("BANKNIFTY", "NSE", expiry_horizon_days=7, spot=46000.0)

        self.assertEqual(len(nifty_snapshot.contracts), 1)
        self.assertEqual(nifty_snapshot.contracts[0].strike, 19500.0)
        self.assertEqual(len(banknifty_snapshot.contracts), 1)
        self.assertEqual(banknifty_snapshot.contracts[0].strike, 45000.0)


if __name__ == "__main__":
    unittest.main()

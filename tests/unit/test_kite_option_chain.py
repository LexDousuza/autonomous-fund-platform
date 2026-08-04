"""
Unit tests for core.market_data.kite_option_chain.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_kite_option_chain.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from core.broker.base import OptionType
from core.market_data.kite_option_chain import KiteOptionChainProvider, fetch_expiries, fetch_option_chain
from core.pricing.black_scholes import price as bs_price

_FUTURE_EXPIRY = (date.today() + timedelta(days=30)).isoformat()
_OTHER_EXPIRY = (date.today() + timedelta(days=58)).isoformat()
_PAST_EXPIRY = (date.today() - timedelta(days=5)).isoformat()
_BEYOND_HORIZON_EXPIRY = (date.today() + timedelta(days=90)).isoformat()


class FakeKiteMarketDataTransport:
    def __init__(self) -> None:
        self.instruments: list[dict] = []
        self.quotes: dict[str, dict] = {}

    def list_instruments(self, exchange: str) -> list[dict]:
        return self.instruments

    def get_quotes(self, symbols_with_exchange: list[tuple[str, str]]) -> dict:
        return {
            f"{exchange}:{symbol}": self.quotes[f"{exchange}:{symbol}"]
            for symbol, exchange in symbols_with_exchange
            if f"{exchange}:{symbol}" in self.quotes
        }


def _instrument_row(
    tradingsymbol: str, name: str, expiry: str, strike: float, instrument_type: str, lot_size: int = 250
) -> dict:
    return {
        "tradingsymbol": tradingsymbol,
        "name": name,
        "expiry": expiry,
        "strike": strike,
        "instrument_type": instrument_type,
        "lot_size": lot_size,
    }


class FetchExpiriesTests(unittest.TestCase):
    def test_returns_sorted_distinct_expiries_for_matching_underlying(self) -> None:
        transport = FakeKiteMarketDataTransport()
        transport.instruments = [
            _instrument_row("NIFTY26AUG23000CE", "NIFTY", _OTHER_EXPIRY, 23000, "CE"),
            _instrument_row("NIFTY26AUG23000PE", "NIFTY", _OTHER_EXPIRY, 23000, "PE"),
            _instrument_row("NIFTY26AUG23100CE", "NIFTY", _FUTURE_EXPIRY, 23100, "CE"),
            _instrument_row("BANKNIFTY26AUG50000CE", "BANKNIFTY", _FUTURE_EXPIRY, 50000, "CE"),
            _instrument_row("NIFTY26AUGFUT", "NIFTY", _FUTURE_EXPIRY, 0, "FUT"),
        ]

        result = fetch_expiries(transport, "NFO", "NIFTY")

        self.assertEqual(result, sorted([_FUTURE_EXPIRY, _OTHER_EXPIRY]))


class FetchOptionChainTests(unittest.TestCase):
    def test_builds_contracts_and_derives_iv_from_quote(self) -> None:
        strike = 2650.0
        rate = 0.07
        true_sigma = 0.25
        time_to_expiry = 30 / 365
        market_price = bs_price(OptionType.CALL, 2650.0, strike, rate, true_sigma, time_to_expiry)

        transport = FakeKiteMarketDataTransport()
        transport.instruments = [
            _instrument_row("RELIANCE26AUG2650CE", "RELIANCE", _FUTURE_EXPIRY, strike, "CE"),
        ]
        transport.quotes = {"NFO:RELIANCE26AUG2650CE": {"last_price": market_price}}

        snapshot = fetch_option_chain(transport, "NFO", "RELIANCE", _FUTURE_EXPIRY, 2650.0, rate)

        self.assertEqual(len(snapshot.contracts), 1)
        contract = snapshot.contracts[0]
        self.assertEqual(contract.strike, strike)
        self.assertEqual(contract.tradingsymbol, "RELIANCE26AUG2650CE")
        self.assertAlmostEqual(contract.last_price, market_price)
        self.assertIsNotNone(contract.iv)
        self.assertAlmostEqual(contract.iv, true_sigma, places=3)
        self.assertIsNotNone(contract.greeks)
        self.assertEqual(contract.lot_size, 250)

    def test_extracts_bid_ask_oi_volume_from_quote_depth(self) -> None:
        transport = FakeKiteMarketDataTransport()
        transport.instruments = [
            _instrument_row("RELIANCE26AUG2650CE", "RELIANCE", _FUTURE_EXPIRY, 2650.0, "CE"),
        ]
        transport.quotes = {
            "NFO:RELIANCE26AUG2650CE": {
                "last_price": 42.0,
                "oi": 125000,
                "volume": 84000,
                "depth": {
                    "buy": [{"price": 41.5, "quantity": 300, "orders": 2}],
                    "sell": [{"price": 42.5, "quantity": 150, "orders": 1}],
                },
            }
        }

        snapshot = fetch_option_chain(transport, "NFO", "RELIANCE", _FUTURE_EXPIRY, 2650.0, 0.07)

        contract = snapshot.contracts[0]
        self.assertEqual(contract.bid, 41.5)
        self.assertEqual(contract.ask, 42.5)
        self.assertEqual(contract.open_interest, 125000)
        self.assertEqual(contract.volume, 84000)

    def test_missing_depth_leaves_bid_ask_none_not_a_crash(self) -> None:
        transport = FakeKiteMarketDataTransport()
        transport.instruments = [
            _instrument_row("RELIANCE26AUG2650CE", "RELIANCE", _FUTURE_EXPIRY, 2650.0, "CE"),
        ]
        transport.quotes = {"NFO:RELIANCE26AUG2650CE": {"last_price": 42.0}}  # no depth/oi/volume at all

        snapshot = fetch_option_chain(transport, "NFO", "RELIANCE", _FUTURE_EXPIRY, 2650.0, 0.07)

        contract = snapshot.contracts[0]
        self.assertIsNone(contract.bid)
        self.assertIsNone(contract.ask)
        self.assertIsNone(contract.open_interest)
        self.assertIsNone(contract.volume)

    def test_empty_depth_side_leaves_that_side_none(self) -> None:
        transport = FakeKiteMarketDataTransport()
        transport.instruments = [
            _instrument_row("RELIANCE26AUG2650CE", "RELIANCE", _FUTURE_EXPIRY, 2650.0, "CE"),
        ]
        transport.quotes = {
            "NFO:RELIANCE26AUG2650CE": {
                "last_price": 42.0,
                "depth": {"buy": [], "sell": [{"price": 42.5, "quantity": 150, "orders": 1}]},
            }
        }

        snapshot = fetch_option_chain(transport, "NFO", "RELIANCE", _FUTURE_EXPIRY, 2650.0, 0.07)

        contract = snapshot.contracts[0]
        self.assertIsNone(contract.bid)  # no resting buy orders - illiquid strike, not a crash
        self.assertEqual(contract.ask, 42.5)

    def test_contract_with_no_quote_has_no_price_or_iv(self) -> None:
        transport = FakeKiteMarketDataTransport()
        transport.instruments = [
            _instrument_row("RELIANCE26AUG2650CE", "RELIANCE", _FUTURE_EXPIRY, 2650.0, "CE"),
        ]
        transport.quotes = {}  # no quote available for this contract

        snapshot = fetch_option_chain(transport, "NFO", "RELIANCE", _FUTURE_EXPIRY, 2650.0, 0.07)

        self.assertEqual(len(snapshot.contracts), 1)
        self.assertIsNone(snapshot.contracts[0].last_price)
        self.assertIsNone(snapshot.contracts[0].iv)

    def test_filters_out_other_underlyings_and_expiries(self) -> None:
        transport = FakeKiteMarketDataTransport()
        transport.instruments = [
            _instrument_row("RELIANCE26AUG2650CE", "RELIANCE", _FUTURE_EXPIRY, 2650.0, "CE"),
            _instrument_row("RELIANCE26SEP2650CE", "RELIANCE", _OTHER_EXPIRY, 2650.0, "CE"),
            _instrument_row("TCS26AUG3900CE", "TCS", _FUTURE_EXPIRY, 3900.0, "CE"),
        ]
        transport.quotes = {}

        snapshot = fetch_option_chain(transport, "NFO", "RELIANCE", _FUTURE_EXPIRY, 2650.0, 0.07)

        self.assertEqual(len(snapshot.contracts), 1)
        self.assertEqual(snapshot.contracts[0].tradingsymbol, "RELIANCE26AUG2650CE")


class KiteOptionChainProviderTests(unittest.TestCase):
    """
    KiteOptionChainProvider.get_chain matches the option_chain_provider
    contract every options strategy expects (see
    core.strategy.options_mispricing's docstring) - underlying_symbol,
    exchange, expiry_horizon_days, spot -> OptionChainSnapshot | None.
    """

    def test_picks_soonest_expiry_within_horizon(self) -> None:
        transport = FakeKiteMarketDataTransport()
        transport.instruments = [
            _instrument_row("NIFTY26AUG23000CE", "NIFTY", _FUTURE_EXPIRY, 23000, "CE"),
            _instrument_row("NIFTY26SEP23000CE", "NIFTY", _OTHER_EXPIRY, 23000, "CE"),
        ]
        provider = KiteOptionChainProvider(transport, risk_free_rate=0.07)

        snapshot = provider.get_chain("NIFTY", "NFO", expiry_horizon_days=90, spot=23000.0)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.expiry, _FUTURE_EXPIRY)

    def test_skips_expiry_beyond_horizon(self) -> None:
        transport = FakeKiteMarketDataTransport()
        transport.instruments = [
            _instrument_row("NIFTY26AUG23000CE", "NIFTY", _BEYOND_HORIZON_EXPIRY, 23000, "CE"),
        ]
        provider = KiteOptionChainProvider(transport, risk_free_rate=0.07)

        snapshot = provider.get_chain("NIFTY", "NFO", expiry_horizon_days=7, spot=23000.0)

        self.assertIsNone(snapshot)

    def test_skips_already_expired_expiry(self) -> None:
        transport = FakeKiteMarketDataTransport()
        transport.instruments = [
            _instrument_row("NIFTY26AUG23000CE", "NIFTY", _PAST_EXPIRY, 23000, "CE"),
            _instrument_row("NIFTY26SEP23000CE", "NIFTY", _FUTURE_EXPIRY, 23000, "CE"),
        ]
        provider = KiteOptionChainProvider(transport, risk_free_rate=0.07)

        snapshot = provider.get_chain("NIFTY", "NFO", expiry_horizon_days=90, spot=23000.0)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.expiry, _FUTURE_EXPIRY)

    def test_no_qualifying_expiry_returns_none_not_a_crash(self) -> None:
        transport = FakeKiteMarketDataTransport()
        transport.instruments = []
        provider = KiteOptionChainProvider(transport, risk_free_rate=0.07)

        snapshot = provider.get_chain("NIFTY", "NFO", expiry_horizon_days=7, spot=23000.0)

        self.assertIsNone(snapshot)

    def test_returned_snapshot_has_real_contracts(self) -> None:
        transport = FakeKiteMarketDataTransport()
        transport.instruments = [
            _instrument_row("BANKNIFTY26AUG50000CE", "BANKNIFTY", _FUTURE_EXPIRY, 50000, "CE"),
        ]
        transport.quotes = {"NFO:BANKNIFTY26AUG50000CE": {"last_price": 320.0}}
        provider = KiteOptionChainProvider(transport, risk_free_rate=0.07)

        snapshot = provider.get_chain("BANKNIFTY", "NFO", expiry_horizon_days=45, spot=50000.0)

        self.assertEqual(len(snapshot.contracts), 1)
        self.assertEqual(snapshot.contracts[0].tradingsymbol, "BANKNIFTY26AUG50000CE")


if __name__ == "__main__":
    unittest.main()

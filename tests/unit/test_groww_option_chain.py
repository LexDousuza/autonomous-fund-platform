"""
Unit tests for core.market_data.groww_option_chain.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_groww_option_chain.py -v
"""
from __future__ import annotations

import unittest

from core.broker.base import OptionType
from core.market_data.errors import MarketDataTransportError
from core.market_data.groww_option_chain import fetch_expiries, fetch_option_chain


class FakeGrowwMarketDataTransport:
    def __init__(self) -> None:
        self.expiries_response: dict = {"expiries": ["2026-08-27", "2026-09-24"]}
        self.option_chain_response: dict = {}

    def get_expiries(self, exchange: str, underlying_symbol: str) -> dict:
        return self.expiries_response

    def get_option_chain(self, exchange: str, underlying: str, expiry_date: str) -> dict:
        return self.option_chain_response


class FetchExpiriesTests(unittest.TestCase):
    def test_returns_expiry_list(self) -> None:
        transport = FakeGrowwMarketDataTransport()
        result = fetch_expiries(transport, "NSE", "RELIANCE")
        self.assertEqual(result, ["2026-08-27", "2026-09-24"])

    def test_missing_field_raises(self) -> None:
        transport = FakeGrowwMarketDataTransport()
        transport.expiries_response = {}
        with self.assertRaises(MarketDataTransportError):
            fetch_expiries(transport, "NSE", "RELIANCE")


class FetchOptionChainTests(unittest.TestCase):
    def test_parses_strikes_into_contracts_with_greeks(self) -> None:
        transport = FakeGrowwMarketDataTransport()
        transport.option_chain_response = {
            "underlying_ltp": 2650.5,
            "strikes": {
                "2600": {
                    "CE": {
                        "trading_symbol": "RELIANCE26AUG2600CE",
                        "ltp": 65.0,
                        "greeks": {"delta": 0.6, "gamma": 0.001, "vega": 2.3, "theta": -1.2, "rho": 0.5, "iv": 0.18},
                    },
                    "PE": {
                        "trading_symbol": "RELIANCE26AUG2600PE",
                        "ltp": 12.0,
                        "greeks": {"delta": -0.4, "gamma": 0.001, "vega": 2.1, "theta": -1.0, "rho": -0.3, "iv": 0.20},
                    },
                },
            },
        }

        snapshot = fetch_option_chain(transport, "NSE", "RELIANCE", "2026-08-27")

        self.assertEqual(snapshot.underlying_last_price, 2650.5)
        self.assertEqual(len(snapshot.contracts), 2)
        call = next(c for c in snapshot.contracts if c.option_type == OptionType.CALL)
        self.assertEqual(call.strike, 2600.0)
        self.assertEqual(call.tradingsymbol, "RELIANCE26AUG2600CE")
        self.assertEqual(call.last_price, 65.0)
        self.assertEqual(call.iv, 0.18)
        self.assertEqual(call.greeks.delta, 0.6)

    def test_missing_side_is_skipped_not_fabricated(self) -> None:
        transport = FakeGrowwMarketDataTransport()
        transport.option_chain_response = {
            "underlying_ltp": 2650.5,
            "strikes": {"2600": {"CE": {"trading_symbol": "X", "ltp": 65.0, "greeks": {}}}},
        }

        snapshot = fetch_option_chain(transport, "NSE", "RELIANCE", "2026-08-27")

        self.assertEqual(len(snapshot.contracts), 1)
        self.assertEqual(snapshot.contracts[0].option_type, OptionType.CALL)

    def test_missing_top_level_field_raises(self) -> None:
        transport = FakeGrowwMarketDataTransport()
        transport.option_chain_response = {"strikes": {}}  # missing underlying_ltp
        with self.assertRaises(MarketDataTransportError):
            fetch_option_chain(transport, "NSE", "RELIANCE", "2026-08-27")


if __name__ == "__main__":
    unittest.main()

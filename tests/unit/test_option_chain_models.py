"""
Unit tests for core.market_data.option_chain_models.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_option_chain_models.py -v
"""
from __future__ import annotations

import unittest

from core.broker.base import OptionType
from core.market_data.option_chain_models import OptionChainContract, OptionChainSnapshot
from core.pricing.black_scholes import Greeks


class OptionChainModelsTests(unittest.TestCase):
    def test_contract_defaults_have_no_greeks_or_iv(self) -> None:
        contract = OptionChainContract(
            underlying_symbol="RELIANCE",
            strike=2600.0,
            expiry="2026-08-27",
            option_type=OptionType.CALL,
            tradingsymbol="RELIANCE26AUG2600CE",
        )
        self.assertIsNone(contract.greeks)
        self.assertIsNone(contract.iv)
        self.assertIsNone(contract.last_price)

    def test_contract_can_carry_full_greeks(self) -> None:
        contract = OptionChainContract(
            underlying_symbol="RELIANCE",
            strike=2600.0,
            expiry="2026-08-27",
            option_type=OptionType.CALL,
            tradingsymbol="RELIANCE26AUG2600CE",
            last_price=55.0,
            greeks=Greeks(delta=0.6, gamma=0.001, vega=2.3, theta=-1.2, rho=0.5),
            iv=0.18,
        )
        self.assertEqual(contract.greeks.delta, 0.6)
        self.assertEqual(contract.iv, 0.18)

    def test_snapshot_defaults_to_empty_contracts(self) -> None:
        snapshot = OptionChainSnapshot(underlying_symbol="RELIANCE", underlying_last_price=2650.0, expiry="2026-08-27")
        self.assertEqual(snapshot.contracts, [])


if __name__ == "__main__":
    unittest.main()

"""
Unit tests for core.pricing.iv_resolver.resolve_atm_iv.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_iv_resolver.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from core.broker.base import OptionType
from core.market_data.option_chain_models import OptionChainContract, OptionChainSnapshot
from core.pricing.black_scholes import price as bs_price
from core.pricing.iv_resolver import resolve_atm_iv

_FUTURE_EXPIRY = (date.today() + timedelta(days=30)).isoformat()


def _contract(strike: float, option_type: OptionType, iv: float | None = None, last_price: float | None = None) -> OptionChainContract:
    return OptionChainContract(
        underlying_symbol="RELIANCE",
        strike=strike,
        expiry=_FUTURE_EXPIRY,
        option_type=option_type,
        tradingsymbol=f"RELIANCE{int(strike)}{option_type.value[0]}E",
        last_price=last_price,
        iv=iv,
    )


class ResolveAtmIvTests(unittest.TestCase):
    def test_empty_snapshot_returns_none(self) -> None:
        snapshot = OptionChainSnapshot(underlying_symbol="RELIANCE", underlying_last_price=2650.0, expiry=_FUTURE_EXPIRY)
        self.assertIsNone(resolve_atm_iv(snapshot, spot=2650.0, risk_free_rate=0.07, as_of=date.today()))

    def test_uses_known_iv_directly_averaging_call_and_put(self) -> None:
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE",
            underlying_last_price=2650.0,
            expiry=_FUTURE_EXPIRY,
            contracts=[
                _contract(2650.0, OptionType.CALL, iv=0.20),
                _contract(2650.0, OptionType.PUT, iv=0.24),
            ],
        )
        result = resolve_atm_iv(snapshot, spot=2650.0, risk_free_rate=0.07, as_of=date.today())
        self.assertAlmostEqual(result, 0.22, places=6)

    def test_picks_nearest_strike_to_spot(self) -> None:
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE",
            underlying_last_price=2650.0,
            expiry=_FUTURE_EXPIRY,
            contracts=[
                _contract(2600.0, OptionType.CALL, iv=0.30),
                _contract(2650.0, OptionType.CALL, iv=0.20),
                _contract(2700.0, OptionType.CALL, iv=0.40),
            ],
        )
        result = resolve_atm_iv(snapshot, spot=2652.0, risk_free_rate=0.07, as_of=date.today())
        self.assertAlmostEqual(result, 0.20, places=6)

    def test_solves_iv_from_last_price_when_iv_missing(self) -> None:
        spot, strike, rate, true_sigma, time_to_expiry = 2650.0, 2650.0, 0.07, 0.25, 30 / 365
        market_price = bs_price(OptionType.CALL, spot, strike, rate, true_sigma, time_to_expiry)
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE",
            underlying_last_price=spot,
            expiry=_FUTURE_EXPIRY,
            contracts=[_contract(strike, OptionType.CALL, iv=None, last_price=market_price)],
        )

        result = resolve_atm_iv(snapshot, spot=spot, risk_free_rate=rate, as_of=date.today())

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, true_sigma, places=3)

    def test_returns_none_when_no_contract_yields_usable_iv(self) -> None:
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE",
            underlying_last_price=2650.0,
            expiry=_FUTURE_EXPIRY,
            contracts=[_contract(2650.0, OptionType.CALL, iv=None, last_price=None)],
        )
        self.assertIsNone(resolve_atm_iv(snapshot, spot=2650.0, risk_free_rate=0.07, as_of=date.today()))


if __name__ == "__main__":
    unittest.main()

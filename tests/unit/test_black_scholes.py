"""Tests for core.pricing.black_scholes: price, greeks, implied_volatility."""
from __future__ import annotations

import unittest

from core.broker.base import OptionType
from core.pricing.black_scholes import greeks, implied_volatility, price


class PriceTests(unittest.TestCase):
    def test_matches_hull_textbook_reference_value(self) -> None:
        # Hull, "Options, Futures, and Other Derivatives": S=42, K=40,
        # r=10%, sigma=20%, T=0.5y -> call ~4.76, put ~0.81.
        call = price(OptionType.CALL, spot=42, strike=40, rate=0.10, sigma=0.20, time_to_expiry=0.5)
        put = price(OptionType.PUT, spot=42, strike=40, rate=0.10, sigma=0.20, time_to_expiry=0.5)
        self.assertAlmostEqual(call, 4.76, places=2)
        self.assertAlmostEqual(put, 0.81, places=2)

    def test_put_call_parity_across_a_grid(self) -> None:
        # C - P == S - K*e^(-rT) for every combination, by construction.
        for spot in (50.0, 100.0, 150.0):
            for strike in (80.0, 100.0, 120.0):
                for rate in (0.02, 0.07):
                    for sigma in (0.1, 0.3, 0.6):
                        for time_to_expiry in (0.1, 1.0, 2.0):
                            call = price(OptionType.CALL, spot, strike, rate, sigma, time_to_expiry)
                            put = price(OptionType.PUT, spot, strike, rate, sigma, time_to_expiry)
                            expected = spot - strike * pow(2.718281828459045, -rate * time_to_expiry)
                            self.assertAlmostEqual(call - put, expected, places=6)

    def test_rejects_nonpositive_inputs(self) -> None:
        with self.assertRaises(ValueError):
            price(OptionType.CALL, spot=0, strike=100, rate=0.05, sigma=0.2, time_to_expiry=1.0)
        with self.assertRaises(ValueError):
            price(OptionType.CALL, spot=100, strike=100, rate=0.05, sigma=0.2, time_to_expiry=0)

    def test_dividend_yield_defaults_to_zero_unchanged_behavior(self) -> None:
        # Every existing caller omits dividend_yield - explicit 0.0 must
        # reproduce the exact same price as not passing it at all.
        with_default = price(OptionType.CALL, spot=100, strike=95, rate=0.07, sigma=0.3, time_to_expiry=0.25)
        with_explicit_zero = price(
            OptionType.CALL, spot=100, strike=95, rate=0.07, sigma=0.3, time_to_expiry=0.25, dividend_yield=0.0
        )
        self.assertEqual(with_default, with_explicit_zero)

    def test_put_call_parity_with_dividend_yield(self) -> None:
        # With a continuous dividend yield q: C - P == S*e^(-qT) - K*e^(-rT).
        for dividend_yield in (0.01, 0.03, 0.08):
            call = price(OptionType.CALL, spot=100, strike=95, rate=0.07, sigma=0.3, time_to_expiry=0.5, dividend_yield=dividend_yield)
            put = price(OptionType.PUT, spot=100, strike=95, rate=0.07, sigma=0.3, time_to_expiry=0.5, dividend_yield=dividend_yield)
            expected = 100 * pow(2.718281828459045, -dividend_yield * 0.5) - 95 * pow(2.718281828459045, -0.07 * 0.5)
            self.assertAlmostEqual(call - put, expected, places=6)

    def test_higher_dividend_yield_reduces_call_price(self) -> None:
        # A dividend paid out lowers the expected forward price a call
        # holder benefits from - a higher yield must never increase it.
        low_q = price(OptionType.CALL, spot=100, strike=100, rate=0.07, sigma=0.25, time_to_expiry=0.5, dividend_yield=0.01)
        high_q = price(OptionType.CALL, spot=100, strike=100, rate=0.07, sigma=0.25, time_to_expiry=0.5, dividend_yield=0.06)
        self.assertLess(high_q, low_q)

    def test_higher_dividend_yield_increases_put_price(self) -> None:
        low_q = price(OptionType.PUT, spot=100, strike=100, rate=0.07, sigma=0.25, time_to_expiry=0.5, dividend_yield=0.01)
        high_q = price(OptionType.PUT, spot=100, strike=100, rate=0.07, sigma=0.25, time_to_expiry=0.5, dividend_yield=0.06)
        self.assertGreater(high_q, low_q)


class GreeksTests(unittest.TestCase):
    def test_call_delta_and_put_delta_are_in_expected_ranges(self) -> None:
        call_greeks = greeks(OptionType.CALL, spot=100, strike=100, rate=0.05, sigma=0.25, time_to_expiry=1.0)
        put_greeks = greeks(OptionType.PUT, spot=100, strike=100, rate=0.05, sigma=0.25, time_to_expiry=1.0)

        self.assertTrue(0 < call_greeks.delta < 1)
        self.assertTrue(-1 < put_greeks.delta < 0)
        self.assertAlmostEqual(call_greeks.delta - put_greeks.delta, 1.0, places=6)

    def test_gamma_and_vega_are_positive_and_shared_between_call_and_put(self) -> None:
        call_greeks = greeks(OptionType.CALL, spot=100, strike=95, rate=0.05, sigma=0.3, time_to_expiry=0.5)
        put_greeks = greeks(OptionType.PUT, spot=100, strike=95, rate=0.05, sigma=0.3, time_to_expiry=0.5)

        self.assertGreater(call_greeks.gamma, 0)
        self.assertGreater(call_greeks.vega, 0)
        self.assertAlmostEqual(call_greeks.gamma, put_greeks.gamma, places=8)
        self.assertAlmostEqual(call_greeks.vega, put_greeks.vega, places=8)


class ImpliedVolatilityTests(unittest.TestCase):
    def test_round_trips_price_to_sigma(self) -> None:
        for option_type in (OptionType.CALL, OptionType.PUT):
            true_sigma = 0.35
            market_price = price(option_type, spot=100, strike=105, rate=0.06, sigma=true_sigma, time_to_expiry=0.75)
            recovered_sigma = implied_volatility(
                option_type, market_price, spot=100, strike=105, rate=0.06, time_to_expiry=0.75
            )
            self.assertIsNotNone(recovered_sigma)
            self.assertAlmostEqual(recovered_sigma, true_sigma, places=4)

    def test_returns_none_outside_arbitrage_free_bracket(self) -> None:
        # A call can never be worth more than spot itself.
        result = implied_volatility(
            OptionType.CALL, market_price=1000.0, spot=100, strike=100, rate=0.05, time_to_expiry=1.0
        )
        self.assertIsNone(result)

    def test_returns_none_for_nonpositive_market_price(self) -> None:
        result = implied_volatility(
            OptionType.CALL, market_price=0.0, spot=100, strike=100, rate=0.05, time_to_expiry=1.0
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

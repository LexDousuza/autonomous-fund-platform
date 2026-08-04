"""
Unit tests for core.broker.kite_symbols.build_kite_option_tradingsymbol.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_kite_symbols.py -v
"""
from __future__ import annotations

import unittest
from datetime import date

from core.broker.base import OptionType
from core.broker.kite_symbols import build_kite_option_tradingsymbol


class BuildKiteOptionTradingsymbolTests(unittest.TestCase):
    def test_monthly_expiry_uses_three_letter_month_code(self) -> None:
        # 2026-08-27 is the last Thursday of August 2026 (monthly expiry).
        symbol = build_kite_option_tradingsymbol("RELIANCE", date(2026, 8, 27), 2600, OptionType.CALL)
        self.assertEqual(symbol, "RELIANCE26AUG2600CE")

    def test_weekly_expiry_uses_single_digit_month_code_for_jan_to_sep(self) -> None:
        # 2026-08-06 is a Thursday but not the last one in August -> weekly format.
        symbol = build_kite_option_tradingsymbol("NIFTY", date(2026, 8, 6), 23000, OptionType.PUT)
        self.assertEqual(symbol, "NIFTY2680623000PE")

    def test_weekly_expiry_uses_letter_month_code_for_oct_nov_dec(self) -> None:
        symbol_oct = build_kite_option_tradingsymbol("NIFTY", date(2026, 10, 8), 23000, OptionType.CALL)
        self.assertEqual(symbol_oct, "NIFTY26O0823000CE")

        symbol_nov = build_kite_option_tradingsymbol("NIFTY", date(2026, 11, 5), 23000, OptionType.CALL)
        self.assertEqual(symbol_nov, "NIFTY26N0523000CE")

        symbol_dec = build_kite_option_tradingsymbol("NIFTY", date(2026, 12, 3), 23000, OptionType.CALL)
        self.assertEqual(symbol_dec, "NIFTY26D0323000CE")

    def test_non_integer_strike_keeps_decimal_digits(self) -> None:
        symbol = build_kite_option_tradingsymbol("RELIANCE", date(2026, 8, 27), 2652.5, OptionType.PUT)
        self.assertEqual(symbol, "RELIANCE26AUG2652.5PE")

    def test_is_deterministic(self) -> None:
        first = build_kite_option_tradingsymbol("TCS", date(2026, 8, 27), 3900, OptionType.CALL)
        second = build_kite_option_tradingsymbol("TCS", date(2026, 8, 27), 3900, OptionType.CALL)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

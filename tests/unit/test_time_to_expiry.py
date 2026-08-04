"""
Unit tests for core.pricing.time_to_expiry.time_to_expiry_years.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_time_to_expiry.py -v
"""
from __future__ import annotations

import unittest
from datetime import date

from core.pricing.time_to_expiry import time_to_expiry_years


class TimeToExpiryYearsTests(unittest.TestCase):
    def test_computed_relative_to_as_of_not_the_real_calendar(self) -> None:
        # as_of is deliberately far from any real "today" - proves the
        # result depends only on the explicit parameter, never a hidden
        # date.today() call.
        result = time_to_expiry_years("2020-01-08", as_of=date(2020, 1, 1))
        self.assertAlmostEqual(result, 7 / 365.0, places=9)

    def test_same_as_of_and_expiry_is_zero(self) -> None:
        self.assertEqual(time_to_expiry_years("2020-01-01", as_of=date(2020, 1, 1)), 0.0)

    def test_expiry_before_as_of_clamps_to_zero_not_negative(self) -> None:
        self.assertEqual(time_to_expiry_years("2019-12-25", as_of=date(2020, 1, 1)), 0.0)

    def test_far_future_expiry_relative_to_a_far_past_as_of(self) -> None:
        # The specific bug this guards against: a caller must never
        # substitute the real wall-clock date for as_of - this asserts
        # the function itself has no such fallback path at all.
        result = time_to_expiry_years("2020-02-01", as_of=date(2020, 1, 1))
        self.assertAlmostEqual(result, 31 / 365.0, places=9)


if __name__ == "__main__":
    unittest.main()

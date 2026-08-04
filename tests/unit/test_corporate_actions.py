"""
Unit tests for core.market_data.corporate_actions.

Classifier tests use real NSE subject-text samples (RELIANCE, HDFCBANK,
ITC, ADANIENT, TCS, SKFINDIA - fetched 2026-07-30), not invented examples.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_corporate_actions.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from core.market_data.corporate_actions import (
    NEEDS_ADJUSTMENT_NOT_COMPUTED,
    NO_ADJUSTMENT_NEEDED,
    ActionType,
    adjust_bars,
    classify,
    parse_action,
)
from core.strategy.base import MarketBar


def _bar(day: int, price: float) -> MarketBar:
    return MarketBar(
        symbol="TEST", exchange="NSE", timestamp=datetime(2026, 1, day),
        open=price, high=price, low=price, close=price, volume=1000.0,
    )


class ClassifyTests(unittest.TestCase):
    def test_bonus_one_to_one(self) -> None:
        action_type, factor = classify("Bonus 1:1")
        self.assertEqual(action_type, ActionType.BONUS)
        self.assertAlmostEqual(factor, 0.5, places=6)

    def test_bonus_one_to_two(self) -> None:
        # ITC 2016-07-01 - 1 new share per 2 held: 2 shares -> 3 shares.
        action_type, factor = classify(" Bonus 1:2")
        self.assertEqual(action_type, ActionType.BONUS)
        self.assertAlmostEqual(factor, 2 / 3, places=6)

    def test_face_value_split(self) -> None:
        # HDFCBANK 2019-09-19 - real NSE text.
        action_type, factor = classify(
            " Face Value Split (Sub-Division) - From Rs 2 Per Share To Rs 1 Per Share"
        )
        self.assertEqual(action_type, ActionType.SPLIT)
        self.assertAlmostEqual(factor, 0.5, places=6)

    def test_rights_has_no_computed_factor(self) -> None:
        # ADANIENT 2025-11-17 - real NSE text.
        action_type, factor = classify("Rights 3:25 @ Premium Rs 1799/-")
        self.assertEqual(action_type, ActionType.RIGHTS)
        self.assertIsNone(factor)

    def test_demerger_has_no_computed_factor(self) -> None:
        # SKFINDIA 2025-10-15 / ITC 2025-01-06 - real NSE text.
        action_type, factor = classify("Demerger")
        self.assertEqual(action_type, ActionType.DEMERGER)
        self.assertIsNone(factor)

    def test_scheme_of_arrangement_classifies_as_merger(self) -> None:
        # ADANIENT 2018-04-05 - real NSE text.
        action_type, factor = classify("Scheme Of Arrangement")
        self.assertEqual(action_type, ActionType.MERGER)
        self.assertIsNone(factor)

    def test_buyback_variants(self) -> None:
        # TCS - real NSE text has both spellings across different years.
        self.assertEqual(classify("Buyback")[0], ActionType.BUYBACK)
        self.assertEqual(classify("Buy Back")[0], ActionType.BUYBACK)

    def test_dividend(self) -> None:
        action_type, factor = classify(" Dividend - Rs 40 Per Share")
        self.assertEqual(action_type, ActionType.DIVIDEND)
        self.assertIsNone(factor)

    def test_face_value_split_to_one_rupee_uses_re_not_rs(self) -> None:
        # HCLTECH-shaped real NSE text - "Re" (singular), not "Rs", when
        # the target face value is exactly 1. Missed by the first version
        # of this regex (2026-07-30): 19 of 20 real split records in the
        # first live fetch had no computed factor because of this.
        action_type, factor = classify(" Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share")
        self.assertEqual(action_type, ActionType.SPLIT)
        self.assertAlmostEqual(factor, 0.1, places=6)

    def test_face_value_split_without_per_share_or_slash(self) -> None:
        # Real NSE text variant with neither "Per Share" nor "/-".
        action_type, factor = classify("Face Value Split From Rs 10 To Re 2")
        self.assertEqual(action_type, ActionType.SPLIT)
        self.assertAlmostEqual(factor, 0.2, places=6)

    def test_interest_payment_is_not_an_equity_action(self) -> None:
        # Real NSE text (debenture interest) - must not be classified as
        # something that would trigger an exclusion; it's not equity.
        action_type, factor = classify("Interest Payment")
        self.assertEqual(action_type, ActionType.INTEREST_PAYMENT)
        self.assertIsNone(factor)

    def test_abbreviated_interim_dividend_still_classifies_as_dividend(self) -> None:
        # Real NSE text - "Int Div", not the full word "Dividend".
        action_type, factor = classify("Int Div Rs 4 Per Sh")
        self.assertEqual(action_type, ActionType.DIVIDEND)
        self.assertIsNone(factor)

    def test_unrecognized_text_is_other_not_dropped(self) -> None:
        action_type, factor = classify("Something NSE has never published before")
        self.assertEqual(action_type, ActionType.OTHER)
        self.assertIsNone(factor)

    def test_every_action_type_is_categorized_for_adjustment_need(self) -> None:
        for action_type in ActionType:
            if action_type in (ActionType.BONUS, ActionType.SPLIT):
                continue  # these get a computed factor instead
            self.assertTrue(
                action_type in NO_ADJUSTMENT_NEEDED or action_type in NEEDS_ADJUSTMENT_NOT_COMPUTED,
                f"{action_type} isn't in either bucket - adjust_bars can't classify it",
            )


class ParseActionTests(unittest.TestCase):
    def test_builds_dataclass_from_symbol_date_subject(self) -> None:
        action = parse_action("HDFCBANK", date(2025, 8, 26), "Bonus 1:1")
        self.assertEqual(action.symbol, "HDFCBANK")
        self.assertEqual(action.ex_date, date(2025, 8, 26))
        self.assertEqual(action.action_type, ActionType.BONUS)
        self.assertAlmostEqual(action.adjustment_factor, 0.5, places=6)


class AdjustBarsTests(unittest.TestCase):
    def test_bonus_scales_bars_strictly_before_ex_date_only(self) -> None:
        bars = [_bar(1, 100.0), _bar(2, 100.0), _bar(3, 150.0), _bar(4, 150.0)]
        action = parse_action("TEST", date(2026, 1, 3), "Bonus 1:2")  # factor 2/3
        adjusted, unadjustable = adjust_bars(bars, [action])
        self.assertFalse(unadjustable)
        self.assertAlmostEqual(adjusted[0].close, 100.0 * 2 / 3, places=6)
        self.assertAlmostEqual(adjusted[1].close, 100.0 * 2 / 3, places=6)
        self.assertAlmostEqual(adjusted[2].close, 150.0, places=6)  # on/after ex_date: untouched
        self.assertAlmostEqual(adjusted[3].close, 150.0, places=6)

    def test_action_outside_bar_range_is_ignored(self) -> None:
        bars = [_bar(1, 100.0), _bar(2, 100.0)]
        action = parse_action("TEST", date(2026, 6, 1), "Bonus 1:1")  # well after these bars
        adjusted, unadjustable = adjust_bars(bars, [action])
        self.assertFalse(unadjustable)
        self.assertEqual(adjusted[0].close, 100.0)

    def test_demerger_in_range_flags_unadjustable_and_leaves_prices_untouched(self) -> None:
        bars = [_bar(1, 5000.0), _bar(2, 2266.0)]  # the real SKFINDIA-shaped discontinuity
        action = parse_action("SKFINDIA", date(2026, 1, 2), "Demerger")
        adjusted, unadjustable = adjust_bars(bars, [action])
        self.assertTrue(unadjustable)
        self.assertEqual(adjusted[0].close, 5000.0)  # not silently "fixed" with a guess

    def test_dividend_in_range_does_not_flag_unadjustable(self) -> None:
        bars = [_bar(1, 100.0), _bar(2, 98.0)]
        action = parse_action("TEST", date(2026, 1, 2), "Dividend - Rs 2 Per Share")
        adjusted, unadjustable = adjust_bars(bars, [action])
        self.assertFalse(unadjustable)
        self.assertEqual(adjusted[0].close, 100.0)

    def test_two_bonuses_compose_cumulatively(self) -> None:
        bars = [_bar(1, 100.0), _bar(2, 100.0), _bar(3, 100.0)]
        first = parse_action("TEST", date(2026, 1, 2), "Bonus 1:1")  # factor 0.5
        second = parse_action("TEST", date(2026, 1, 3), "Bonus 1:1")  # factor 0.5
        adjusted, unadjustable = adjust_bars(bars, [first, second])
        self.assertFalse(unadjustable)
        # bar 1 is before BOTH ex-dates: scaled by 0.5 * 0.5
        self.assertAlmostEqual(adjusted[0].close, 100.0 * 0.25, places=6)
        # bar 2 is on/after the first ex-date but before the second: scaled by 0.5 only
        self.assertAlmostEqual(adjusted[1].close, 100.0 * 0.5, places=6)
        # bar 3 is on/after both: untouched
        self.assertAlmostEqual(adjusted[2].close, 100.0, places=6)

    def test_empty_bars_returns_empty(self) -> None:
        adjusted, unadjustable = adjust_bars([], [])
        self.assertEqual(adjusted, [])
        self.assertFalse(unadjustable)


if __name__ == "__main__":
    unittest.main()

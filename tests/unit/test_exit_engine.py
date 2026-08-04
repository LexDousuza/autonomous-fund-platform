"""
Unit tests for core.portfolio exit models, ExitPolicyEvaluator, and
PositionManager.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_exit_engine.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, time

from core.portfolio.exit_models import ExitPolicyConfig, PositionSnapshot, parse_time_of_day
from core.portfolio.exits import ExitPolicyEvaluator
from core.portfolio.position_manager import PositionManager
from core.strategy.base import MarketBar


class ParseTimeOfDayTests(unittest.TestCase):
    def test_parses_hh_mm_string(self) -> None:
        self.assertEqual(parse_time_of_day("15:20"), time(15, 20))

    def test_parses_single_digit_hour(self) -> None:
        self.assertEqual(parse_time_of_day("9:15"), time(9, 15))

    def test_malformed_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_time_of_day("not-a-time")


def make_snapshot(
    current_price: float,
    entry_price: float = 100.0,
    current_time: datetime = datetime(2026, 1, 1, 10, 0, 0),
    bars_held: int = 1,
    quantity: int = 10,
    atr: float | None = None,
    best_price_since_entry: float | None = None,
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="TEST",
        exchange="NSE",
        quantity=quantity,
        entry_price=entry_price,
        entry_time=datetime(2026, 1, 1, 9, 15, 0),
        current_price=current_price,
        current_time=current_time,
        bars_held=bars_held,
        strategy_name="test_strategy",
        atr=atr,
        best_price_since_entry=best_price_since_entry,
    )


class ExitPolicyEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluator = ExitPolicyEvaluator()

    def test_stop_loss_triggers_exit(self) -> None:
        policy = ExitPolicyConfig(stop_loss_pct=2.0)
        snapshot = make_snapshot(current_price=97.0, entry_price=100.0)  # -3%

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "stop_loss")
        self.assertEqual(decision.quantity, 10)
        self.assertEqual(decision.exit_price, 97.0)

    def test_stop_loss_not_triggered_above_threshold(self) -> None:
        policy = ExitPolicyConfig(stop_loss_pct=2.0)
        snapshot = make_snapshot(current_price=99.0, entry_price=100.0)  # -1%

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertFalse(decision.should_exit)
        self.assertIsNone(decision.reason)

    def test_profit_target_triggers_exit(self) -> None:
        policy = ExitPolicyConfig(target_pct=3.0)
        snapshot = make_snapshot(current_price=104.0, entry_price=100.0)  # +4%

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "target")

    def test_atr_based_stop_used_instead_of_flat_pct_when_atr_available(self) -> None:
        # flat pct alone would trigger at 97.0 (-3% > 2%); ATR-based stop
        # (2 * 1.0 = 2.0) only triggers at or below 98.0 - proves ATR wins.
        policy = ExitPolicyConfig(stop_loss_pct=2.0, stop_loss_atr_multiplier=2.0)
        snapshot = make_snapshot(current_price=98.5, entry_price=100.0, atr=1.0)

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertFalse(decision.should_exit)

    def test_atr_based_stop_triggers_at_its_own_threshold(self) -> None:
        policy = ExitPolicyConfig(stop_loss_pct=2.0, stop_loss_atr_multiplier=2.0)
        snapshot = make_snapshot(current_price=97.5, entry_price=100.0, atr=1.0)  # 100 - 2*1.0 = 98

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "stop_loss")
        self.assertEqual(decision.metadata["stop_price"], 98.0)

    def test_atr_multiplier_configured_but_no_atr_supplied_falls_back_to_pct(self) -> None:
        policy = ExitPolicyConfig(stop_loss_pct=2.0, stop_loss_atr_multiplier=2.0)
        snapshot = make_snapshot(current_price=97.0, entry_price=100.0, atr=None)  # -3%, pct rule fires

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "stop_loss")
        self.assertEqual(decision.metadata["stop_price"], 98.0)  # flat 2% of 100

    def test_trailing_stop_does_not_fire_before_any_favorable_move(self) -> None:
        # best_price_since_entry == entry_price (no favorable move yet) -
        # a 2% trailing stop here is just an ordinary 2% stop.
        policy = ExitPolicyConfig(trailing_stop_pct=2.0)
        snapshot = make_snapshot(current_price=99.0, entry_price=100.0, best_price_since_entry=100.0)  # -1%

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertFalse(decision.should_exit)

    def test_trailing_stop_triggers_on_pullback_from_the_high_water_mark(self) -> None:
        # Ran up to 110 (best_price_since_entry), pulled back to 107.5 -
        # more than 2% off the high (107.8), even though still well above entry.
        policy = ExitPolicyConfig(trailing_stop_pct=2.0)
        snapshot = make_snapshot(current_price=107.5, entry_price=100.0, best_price_since_entry=110.0)

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "trailing_stop")

    def test_trailing_stop_not_triggered_within_band_of_the_high(self) -> None:
        policy = ExitPolicyConfig(trailing_stop_pct=2.0)
        snapshot = make_snapshot(current_price=108.5, entry_price=100.0, best_price_since_entry=110.0)  # -1.4% off high

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertFalse(decision.should_exit)

    def test_trailing_stop_short_position_mirrors_long(self) -> None:
        # Short from 100, ran favorably down to 90 (best_price_since_entry),
        # bounced back up more than 2% off that low -> exit.
        policy = ExitPolicyConfig(trailing_stop_pct=2.0)
        snapshot = make_snapshot(
            current_price=92.5, entry_price=100.0, quantity=-10, best_price_since_entry=90.0
        )

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "trailing_stop")

    def test_trailing_stop_ignored_when_best_price_not_supplied(self) -> None:
        # A caller not tracking best_price_since_entry (None) must not crash.
        policy = ExitPolicyConfig(trailing_stop_pct=2.0)
        snapshot = make_snapshot(current_price=90.0, entry_price=100.0, best_price_since_entry=None)

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertFalse(decision.should_exit)

    def test_square_off_time_triggers_exit(self) -> None:
        policy = ExitPolicyConfig(square_off_time=time(15, 20))
        snapshot = make_snapshot(
            current_price=100.5,
            current_time=datetime(2026, 1, 1, 15, 20, 0),
        )

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "square_off")

    def test_square_off_time_not_yet_reached(self) -> None:
        policy = ExitPolicyConfig(square_off_time=time(15, 20))
        snapshot = make_snapshot(
            current_price=100.5,
            current_time=datetime(2026, 1, 1, 14, 0, 0),
        )

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertFalse(decision.should_exit)

    def test_max_holding_bars_triggers_exit(self) -> None:
        policy = ExitPolicyConfig(max_holding_bars=3)
        snapshot = make_snapshot(current_price=100.2, bars_held=3)

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "max_holding_bars")

    def test_max_holding_bars_not_yet_reached(self) -> None:
        policy = ExitPolicyConfig(max_holding_bars=3)
        snapshot = make_snapshot(current_price=100.2, bars_held=2)

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertFalse(decision.should_exit)

    def test_no_exit_when_no_rules_configured(self) -> None:
        policy = ExitPolicyConfig()
        snapshot = make_snapshot(current_price=500.0)  # would trip everything if configured

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertFalse(decision.should_exit)

    def test_stop_loss_takes_priority_over_target(self) -> None:
        # A single bar cannot realistically hit both, but priority should
        # still be deterministic: stop loss is checked first.
        policy = ExitPolicyConfig(stop_loss_pct=1.0, target_pct=1.0)
        snapshot = make_snapshot(current_price=98.0, entry_price=100.0)

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertEqual(decision.reason, "stop_loss")


class ShortPositionExitTests(unittest.TestCase):
    """A short (negative quantity) position's stop/target are the mirror image of a long's."""

    def setUp(self) -> None:
        self.evaluator = ExitPolicyEvaluator()

    def test_stop_loss_triggers_on_price_rising(self) -> None:
        # Short at 100: a rising price is a loss, unlike a long.
        policy = ExitPolicyConfig(stop_loss_pct=2.0)
        snapshot = make_snapshot(current_price=103.0, entry_price=100.0, quantity=-10)  # +3%

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "stop_loss")

    def test_stop_loss_not_triggered_below_threshold(self) -> None:
        policy = ExitPolicyConfig(stop_loss_pct=2.0)
        snapshot = make_snapshot(current_price=101.0, entry_price=100.0, quantity=-10)  # +1%, within tolerance

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertFalse(decision.should_exit)

    def test_target_triggers_on_price_falling(self) -> None:
        # Short at 100: a falling price is a profit, unlike a long.
        policy = ExitPolicyConfig(target_pct=2.0)
        snapshot = make_snapshot(current_price=97.0, entry_price=100.0, quantity=-10)  # -3%

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "target")

    def test_exit_decision_preserves_negative_quantity(self) -> None:
        policy = ExitPolicyConfig(stop_loss_pct=2.0)
        snapshot = make_snapshot(current_price=103.0, entry_price=100.0, quantity=-10)

        decision = self.evaluator.evaluate(snapshot, policy)

        self.assertEqual(decision.quantity, -10)

    def test_long_position_unaffected_by_short_branch(self) -> None:
        # Regression guard: a long position (quantity > 0) must still use
        # the original long-only formulas exactly.
        policy = ExitPolicyConfig(stop_loss_pct=2.0, target_pct=2.0)

        falling = make_snapshot(current_price=97.0, entry_price=100.0, quantity=10)
        self.assertEqual(self.evaluator.evaluate(falling, policy).reason, "stop_loss")

        rising = make_snapshot(current_price=103.0, entry_price=100.0, quantity=10)
        self.assertEqual(self.evaluator.evaluate(rising, policy).reason, "target")


class PositionManagerTests(unittest.TestCase):
    def make_bar(self, close: float, ts: datetime) -> MarketBar:
        return MarketBar(
            symbol="TEST",
            exchange="NSE",
            timestamp=ts,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1000.0,
        )

    def test_no_tracked_position_returns_no_decisions(self) -> None:
        manager = PositionManager()
        bar = self.make_bar(100.0, datetime(2026, 1, 1, 9, 20, 0))

        decisions = manager.evaluate_exits(bar, ExitPolicyConfig(stop_loss_pct=1.0))

        self.assertEqual(decisions, [])

    def test_tracked_position_exits_on_stop_loss(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST",
            exchange="NSE",
            quantity=5,
            entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0),
            strategy_name="test_strategy",
        )

        bar = self.make_bar(95.0, datetime(2026, 1, 1, 9, 16, 0))
        decisions = manager.evaluate_exits(bar, ExitPolicyConfig(stop_loss_pct=2.0))

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].reason, "stop_loss")
        self.assertEqual(decisions[0].quantity, 5)

    def test_atr_argument_is_threaded_through_to_the_evaluator(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST",
            exchange="NSE",
            quantity=5,
            entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0),
            strategy_name="test_strategy",
        )
        # flat pct alone (2%) would not trigger at 98.5; ATR-based stop
        # (2 * 1.0 = 2.0 -> stop at 98.0) does - proves atr reached the evaluator.
        bar = self.make_bar(97.5, datetime(2026, 1, 1, 9, 16, 0))
        policy = ExitPolicyConfig(stop_loss_pct=2.0, stop_loss_atr_multiplier=2.0)

        decisions = manager.evaluate_exits(bar, policy, atr=1.0)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].reason, "stop_loss")

    def test_bars_held_increments_across_calls(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST",
            exchange="NSE",
            quantity=5,
            entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0),
            strategy_name="test_strategy",
        )
        policy = ExitPolicyConfig(max_holding_bars=2)

        first = manager.evaluate_exits(
            self.make_bar(100.1, datetime(2026, 1, 1, 9, 16, 0)), policy
        )
        self.assertEqual(first, [])

        second = manager.evaluate_exits(
            self.make_bar(100.1, datetime(2026, 1, 1, 9, 17, 0)), policy
        )
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].reason, "max_holding_bars")

    def test_best_price_since_entry_tracks_the_running_high_across_bars(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST",
            exchange="NSE",
            quantity=5,
            entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0),
            strategy_name="test_strategy",
        )
        policy = ExitPolicyConfig(trailing_stop_pct=2.0)

        # Runs up to 110, then pulls back to 108 (within 2% of the high) - no exit yet.
        manager.evaluate_exits(self.make_bar(110.0, datetime(2026, 1, 1, 9, 16, 0)), policy)
        still_open = manager.evaluate_exits(self.make_bar(108.0, datetime(2026, 1, 1, 9, 17, 0)), policy)
        self.assertEqual(still_open, [])

        # Pulls back further, past 2% off the 110 high (107.8) - the high-water
        # mark must still be 110 (not reset to 108), so this now triggers.
        triggered = manager.evaluate_exits(self.make_bar(107.5, datetime(2026, 1, 1, 9, 18, 0)), policy)
        self.assertEqual(len(triggered), 1)
        self.assertEqual(triggered[0].reason, "trailing_stop")

    def test_owned_by_other_strategy_is_none_when_symbol_untracked(self) -> None:
        manager = PositionManager()

        self.assertIsNone(manager.owned_by_other_strategy("TEST", "NSE", "strategy_a"))

    def test_owned_by_other_strategy_is_none_for_the_owning_strategy(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST",
            exchange="NSE",
            quantity=5,
            entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0),
            strategy_name="strategy_a",
        )

        self.assertIsNone(manager.owned_by_other_strategy("TEST", "NSE", "strategy_a"))

    def test_owned_by_other_strategy_returns_the_owner_for_a_different_strategy(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST",
            exchange="NSE",
            quantity=5,
            entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0),
            strategy_name="strategy_a",
        )

        self.assertEqual(manager.owned_by_other_strategy("TEST", "NSE", "strategy_b"), "strategy_a")

    def test_owned_by_other_strategy_is_none_again_once_cleared(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST",
            exchange="NSE",
            quantity=5,
            entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0),
            strategy_name="strategy_a",
        )
        manager.clear("TEST", "NSE")

        self.assertIsNone(manager.owned_by_other_strategy("TEST", "NSE", "strategy_b"))

    def test_clear_stops_tracking(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST",
            exchange="NSE",
            quantity=5,
            entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0),
            strategy_name="test_strategy",
        )
        manager.clear("TEST", "NSE")

        self.assertFalse(manager.has_open_position("TEST", "NSE"))
        decisions = manager.evaluate_exits(
            self.make_bar(50.0, datetime(2026, 1, 1, 9, 16, 0)),
            ExitPolicyConfig(stop_loss_pct=1.0),
        )
        self.assertEqual(decisions, [])

    def test_reduce_quantity_shrinks_a_long_position_toward_zero(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST", exchange="NSE", quantity=10, entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0), strategy_name="test_strategy",
        )

        manager.reduce_quantity("TEST", "NSE", 6)

        self.assertEqual(manager.open_positions()["NSE:TEST"].quantity, 4)

    def test_reduce_quantity_shrinks_a_short_position_toward_zero(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST", exchange="NSE", quantity=-10, entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0), strategy_name="test_strategy",
        )

        manager.reduce_quantity("TEST", "NSE", 6)

        self.assertEqual(manager.open_positions()["NSE:TEST"].quantity, -4)

    def test_reduce_quantity_to_exactly_zero_stops_tracking(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST", exchange="NSE", quantity=10, entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0), strategy_name="test_strategy",
        )

        manager.reduce_quantity("TEST", "NSE", 10)

        self.assertFalse(manager.has_open_position("TEST", "NSE"))

    def test_reduce_quantity_preserves_bars_held_and_best_price(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST", exchange="NSE", quantity=10, entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0), strategy_name="test_strategy",
        )
        manager.evaluate_exits(self.make_bar(110.0, datetime(2026, 1, 1, 9, 16, 0)), ExitPolicyConfig())

        manager.reduce_quantity("TEST", "NSE", 4)

        remaining = manager.open_positions()["NSE:TEST"]
        self.assertEqual(remaining.bars_held, 1)
        self.assertEqual(remaining.best_price_since_entry, 110.0)

    def test_reduce_quantity_on_an_untracked_symbol_is_a_noop(self) -> None:
        manager = PositionManager()

        manager.reduce_quantity("TEST", "NSE", 5)  # must not raise

        self.assertFalse(manager.has_open_position("TEST", "NSE"))

    def test_register_entry_defaults_group_id_to_none(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST", exchange="NSE", quantity=5, entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0), strategy_name="test_strategy",
        )

        self.assertIsNone(manager.open_positions()["NSE:TEST"].group_id)

    def test_register_entry_accepts_a_group_id(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST", exchange="NSE", quantity=5, entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0), strategy_name="test_strategy",
            group_id="spread-1",
        )

        self.assertEqual(manager.open_positions()["NSE:TEST"].group_id, "spread-1")

    def test_last_price_initializes_to_entry_price(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST", exchange="NSE", quantity=5, entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0), strategy_name="test_strategy",
        )

        self.assertEqual(manager.open_positions()["NSE:TEST"].last_price, 100.0)

    def test_last_price_updates_on_evaluate_exits(self) -> None:
        manager = PositionManager()
        manager.register_entry(
            symbol="TEST", exchange="NSE", quantity=5, entry_price=100.0,
            entry_time=datetime(2026, 1, 1, 9, 15, 0), strategy_name="test_strategy",
        )

        manager.evaluate_exits(self.make_bar(105.0, datetime(2026, 1, 1, 9, 16, 0)), ExitPolicyConfig())

        self.assertEqual(manager.open_positions()["NSE:TEST"].last_price, 105.0)


if __name__ == "__main__":
    unittest.main()

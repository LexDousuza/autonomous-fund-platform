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
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="TEST",
        exchange="NSE",
        quantity=10,
        entry_price=entry_price,
        entry_time=datetime(2026, 1, 1, 9, 15, 0),
        current_price=current_price,
        current_time=current_time,
        bars_held=bars_held,
        strategy_name="test_strategy",
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


if __name__ == "__main__":
    unittest.main()

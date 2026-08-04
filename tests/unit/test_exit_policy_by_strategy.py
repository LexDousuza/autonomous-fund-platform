"""
Unit tests for apps.live_trader.main.build_exit_policy_for_strategy: a
strategy whose own documented design requires relying on square_off_time
alone (because a generic flat stop_loss_pct/target_pct can de-hedge a
multi-leg spread, or is miscalibrated for option-premium dynamics) must
actually get that policy, not the platform's generic 1.5%/2.0% default -
see core/strategy/credit_spread.py's and
core/strategy/options_mispricing.py's docstrings.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_exit_policy_by_strategy.py -v
"""
from __future__ import annotations

import unittest
from datetime import time

from apps.live_trader.main import build_exit_policy_for_strategy
from apps.paper_trader.config import PaperTraderConfig
from apps.paper_trader.main import build_paper_session

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
UNIVERSE_PATH = "config/instruments/india_cash_universe.yaml"

DEFAULT_CONFIG_KWARGS = dict(
    market_data_provider="memory",
    intraday_capital_override=None,
    risk_appetite_override=None,
    historical_from_date="2026-01-01",
    historical_to_date="2026-01-31",
    historical_interval="1d",
)


class BuildPaperSessionUsesTheRightExitPolicyTests(unittest.TestCase):
    def test_options_mispricing_session_gets_square_off_only(self) -> None:
        config = PaperTraderConfig(
            **{**DEFAULT_CONFIG_KWARGS, "strategy_config_path": "config/strategies/options_mispricing.yaml"}
        )

        ctx = build_paper_session(mandate_path=MANDATE_PATH, universe_path=UNIVERSE_PATH, config=config)

        self.assertIsNone(ctx.session_config.exit_policy.stop_loss_pct)
        self.assertIsNone(ctx.session_config.exit_policy.target_pct)
        self.assertIsNotNone(ctx.session_config.exit_policy.square_off_time)

    def test_default_strategy_session_keeps_the_flat_default(self) -> None:
        config = PaperTraderConfig(**DEFAULT_CONFIG_KWARGS)  # default: simple_breakout_momentum

        ctx = build_paper_session(mandate_path=MANDATE_PATH, universe_path=UNIVERSE_PATH, config=config)

        self.assertEqual(ctx.session_config.exit_policy.stop_loss_pct, 1.5)
        self.assertEqual(ctx.session_config.exit_policy.target_pct, 2.0)


class BuildExitPolicyForStrategyTests(unittest.TestCase):
    def test_ordinary_strategy_keeps_the_generic_default(self) -> None:
        policy = build_exit_policy_for_strategy("simple_breakout_momentum", square_off_time=time(15, 10))

        self.assertEqual(policy.stop_loss_pct, 1.5)
        self.assertEqual(policy.target_pct, 2.0)
        self.assertEqual(policy.square_off_time, time(15, 10))

    def test_options_mispricing_relies_on_square_off_time_only(self) -> None:
        policy = build_exit_policy_for_strategy("options_mispricing", square_off_time=time(15, 10))

        self.assertIsNone(policy.stop_loss_pct)
        self.assertIsNone(policy.target_pct)
        self.assertIsNone(policy.trailing_stop_pct)
        self.assertEqual(policy.square_off_time, time(15, 10))

    def test_vertical_credit_spread_relies_on_square_off_time_only(self) -> None:
        # Matches its own documented requirement even though (per the
        # separate multi-day-holding guard) it can't actually run a
        # session yet - correct in advance of that being resolved.
        policy = build_exit_policy_for_strategy("vertical_credit_spread", square_off_time=time(15, 10))

        self.assertIsNone(policy.stop_loss_pct)
        self.assertIsNone(policy.target_pct)

    def test_square_off_time_none_is_passed_through_unchanged(self) -> None:
        policy = build_exit_policy_for_strategy("simple_breakout_momentum", square_off_time=None)

        self.assertIsNone(policy.square_off_time)


if __name__ == "__main__":
    unittest.main()

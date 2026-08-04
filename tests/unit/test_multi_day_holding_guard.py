"""
Unit tests for the multi-day-holding-period guard: strategies designed to
hold a position across multiple days (e.g. vertical_credit_spread selling
premium against a multi-day expiry to capture theta decay) must fail
loudly at session-build time, not silently run same-day-only - see
apps/live_trader/main.py's MultiDayHoldingNotSupportedError.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_multi_day_holding_guard.py -v
"""
from __future__ import annotations

import unittest

from apps.live_trader.main import (
    MultiDayHoldingNotSupportedError,
    check_holding_period_is_supported,
)
from apps.paper_trader.config import PaperTraderConfig
from apps.paper_trader.main import build_paper_portfolio_session, build_paper_session

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


class CheckHoldingPeriodIsSupportedTests(unittest.TestCase):
    def test_vertical_credit_spread_raises(self) -> None:
        with self.assertRaises(MultiDayHoldingNotSupportedError):
            check_holding_period_is_supported("vertical_credit_spread")

    def test_ordinary_intraday_strategy_is_unaffected(self) -> None:
        check_holding_period_is_supported("simple_breakout_momentum")  # must not raise
        check_holding_period_is_supported("momentum")  # must not raise


class BuildPaperSessionRejectsMultiDayStrategyTests(unittest.TestCase):
    def test_single_strategy_session_refuses_credit_spread(self) -> None:
        config = PaperTraderConfig(
            **{**DEFAULT_CONFIG_KWARGS, "strategy_config_path": "config/strategies/vertical_credit_spread.yaml"}
        )
        with self.assertRaises(MultiDayHoldingNotSupportedError):
            build_paper_session(mandate_path=MANDATE_PATH, universe_path=UNIVERSE_PATH, config=config)

    def test_ordinary_strategy_session_still_builds(self) -> None:
        config = PaperTraderConfig(**DEFAULT_CONFIG_KWARGS)  # default: simple_breakout_momentum
        ctx = build_paper_session(mandate_path=MANDATE_PATH, universe_path=UNIVERSE_PATH, config=config)
        self.assertEqual(ctx.session_config.strategy_name, "simple_breakout_momentum")

    def test_portfolio_session_refuses_if_any_active_strategy_is_multi_day(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            active_path = Path(tmp) / "active_portfolio.yaml"
            active_path.write_text(
                "strategies:\n"
                "  - config/strategies/simple_breakout_momentum.yaml\n"
                "  - config/strategies/vertical_credit_spread.yaml\n",
                encoding="utf-8",
            )
            capital_path = Path(tmp) / "approved_capital.yaml"
            capital_path.write_text("approved_capital: 50000.0\n", encoding="utf-8")

            with self.assertRaises(MultiDayHoldingNotSupportedError):
                build_paper_portfolio_session(
                    mandate_path=MANDATE_PATH,
                    universe_path=UNIVERSE_PATH,
                    active_portfolio_path=str(active_path),
                    approved_capital_path=str(capital_path),
                    config=PaperTraderConfig(**DEFAULT_CONFIG_KWARGS),
                )


if __name__ == "__main__":
    unittest.main()

"""
Unit tests for apps.live_trader.config's typed session config + safety
gating (Live Shadow Session Hardening V1).

These tests exercise ``load_live_trader_config_from_env()`` in isolation
- pure env-var parsing/validation, no mandate/universe/broker/market-data
construction. App-level wiring (e.g. "credentials only loaded when
needed") is covered by tests/unit/test_live_trader.py.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_live_trader_config.py -v
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from apps.live_trader.config import (
    LIVE_CONFIRM_REQUIRED_VALUE,
    LiveTradingNotConfirmedError,
    load_live_trader_config_from_env,
)

# Env vars this module's loader reads; cleared before each scenario so
# tests are independent of whatever happens to be in the real environment.
_ALL_LIVE_TRADER_ENV_VARS = (
    "LIVE_BROKER_PROVIDER",
    "LIVE_MARKET_DATA_PROVIDER",
    "LIVE_UNIVERSE_EXCHANGE",
    "LIVE_DRY_RUN",
    "LIVE_MAX_BARS",
    "LIVE_CONFIRM",
)


def _clear_live_trader_env() -> None:
    for var in _ALL_LIVE_TRADER_ENV_VARS:
        os.environ.pop(var, None)


class LoadLiveTraderConfigFromEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        _clear_live_trader_env()

    # -- allowed combinations -----------------------------------------------

    def test_default_is_paper_broker_and_memory_data(self) -> None:
        config = load_live_trader_config_from_env()

        self.assertEqual(config.broker_provider, "paper")
        self.assertEqual(config.broker_mode, "paper")
        self.assertEqual(config.market_data_provider, "memory")
        self.assertFalse(config.requires_groww_credentials)
        self.assertFalse(config.requires_live_confirmation)

    def test_paper_broker_with_groww_data_is_allowed(self) -> None:
        os.environ["LIVE_MARKET_DATA_PROVIDER"] = "groww"

        config = load_live_trader_config_from_env()

        self.assertEqual(config.broker_provider, "paper")
        self.assertEqual(config.broker_mode, "paper")
        self.assertEqual(config.market_data_provider, "groww")
        self.assertTrue(config.requires_groww_credentials)
        self.assertFalse(config.requires_live_confirmation)  # paper broker: no confirmation needed

    def test_groww_broker_with_groww_data_and_confirmation_is_allowed(self) -> None:
        os.environ["LIVE_BROKER_PROVIDER"] = "groww"
        os.environ["LIVE_MARKET_DATA_PROVIDER"] = "groww"
        os.environ["LIVE_CONFIRM"] = LIVE_CONFIRM_REQUIRED_VALUE

        config = load_live_trader_config_from_env()

        self.assertEqual(config.broker_provider, "groww")
        self.assertEqual(config.broker_mode, "live")
        self.assertEqual(config.market_data_provider, "groww")
        self.assertTrue(config.requires_groww_credentials)
        self.assertTrue(config.requires_live_confirmation)
        self.assertTrue(config.live_confirmed)

    # -- the hard safety gate -------------------------------------------------

    def test_groww_live_broker_without_confirmation_raises(self) -> None:
        os.environ["LIVE_BROKER_PROVIDER"] = "groww"
        os.environ["LIVE_MARKET_DATA_PROVIDER"] = "groww"
        # LIVE_CONFIRM intentionally left unset.

        with self.assertRaises(LiveTradingNotConfirmedError) as ctx:
            load_live_trader_config_from_env()

        self.assertIn("LIVE_CONFIRM", str(ctx.exception))

    def test_groww_live_broker_with_wrong_confirmation_value_raises(self) -> None:
        os.environ["LIVE_BROKER_PROVIDER"] = "groww"
        os.environ["LIVE_MARKET_DATA_PROVIDER"] = "groww"
        os.environ["LIVE_CONFIRM"] = "yes please"  # not the exact required string

        with self.assertRaises(LiveTradingNotConfirmedError):
            load_live_trader_config_from_env()

    def test_paper_broker_never_requires_confirmation_even_if_unset(self) -> None:
        os.environ["LIVE_MARKET_DATA_PROVIDER"] = "groww"
        # No LIVE_CONFIRM at all - must not matter for a paper broker.

        config = load_live_trader_config_from_env()  # must not raise

        self.assertEqual(config.broker_provider, "paper")

    # -- invalid provider / mode / combination -------------------------------

    def test_unsupported_broker_provider_raises(self) -> None:
        os.environ["LIVE_BROKER_PROVIDER"] = "zerodha"

        with self.assertRaises(ValueError) as ctx:
            load_live_trader_config_from_env()

        self.assertIn("LIVE_BROKER_PROVIDER", str(ctx.exception))

    def test_unsupported_market_data_provider_raises(self) -> None:
        os.environ["LIVE_MARKET_DATA_PROVIDER"] = "polygon"

        with self.assertRaises(ValueError) as ctx:
            load_live_trader_config_from_env()

        self.assertIn("LIVE_MARKET_DATA_PROVIDER", str(ctx.exception))

    def test_groww_broker_with_memory_data_is_rejected_as_invalid_combination(self) -> None:
        # Real broker against fake demo bars is not a supported safety
        # mode - it's dangerous (real orders driven by synthetic data).
        os.environ["LIVE_BROKER_PROVIDER"] = "groww"
        os.environ["LIVE_MARKET_DATA_PROVIDER"] = "memory"
        os.environ["LIVE_CONFIRM"] = LIVE_CONFIRM_REQUIRED_VALUE

        with self.assertRaises(ValueError) as ctx:
            load_live_trader_config_from_env()

        self.assertIn("combination", str(ctx.exception))

    def test_invalid_combination_is_rejected_before_confirmation_is_even_checked(self) -> None:
        os.environ["LIVE_BROKER_PROVIDER"] = "groww"
        os.environ["LIVE_MARKET_DATA_PROVIDER"] = "memory"
        # No LIVE_CONFIRM set either - but the combination error must win,
        # since it's the more fundamental problem.

        with self.assertRaises(ValueError):
            load_live_trader_config_from_env()

    def test_invalid_max_bars_raises(self) -> None:
        os.environ["LIVE_MAX_BARS"] = "not-a-number"

        with self.assertRaises(ValueError) as ctx:
            load_live_trader_config_from_env()

        self.assertIn("LIVE_MAX_BARS", str(ctx.exception))

    # -- normalization --------------------------------------------------------

    def test_provider_values_are_case_and_whitespace_normalized(self) -> None:
        os.environ["LIVE_BROKER_PROVIDER"] = "  Paper  "
        os.environ["LIVE_MARKET_DATA_PROVIDER"] = " GROWW "

        config = load_live_trader_config_from_env()

        self.assertEqual(config.broker_provider, "paper")
        self.assertEqual(config.market_data_provider, "groww")

    def test_universe_exchange_defaults_and_normalizes(self) -> None:
        config = load_live_trader_config_from_env()
        self.assertEqual(config.universe_exchange, "NSE")

        os.environ["LIVE_UNIVERSE_EXCHANGE"] = " bse "
        config = load_live_trader_config_from_env()
        self.assertEqual(config.universe_exchange, "BSE")

    # -- dry run / max bars ----------------------------------------------------

    def test_dry_run_defaults_false_and_parses_truthy_values(self) -> None:
        self.assertFalse(load_live_trader_config_from_env().dry_run)

        for truthy in ("1", "true", "True", "yes", "on"):
            os.environ["LIVE_DRY_RUN"] = truthy
            self.assertTrue(load_live_trader_config_from_env().dry_run, msg=truthy)

        for falsy in ("0", "false", "no", "off", ""):
            os.environ["LIVE_DRY_RUN"] = falsy
            self.assertFalse(load_live_trader_config_from_env().dry_run, msg=falsy)

    def test_max_bars_defaults_none_and_parses_when_set(self) -> None:
        self.assertIsNone(load_live_trader_config_from_env().max_bars)

        os.environ["LIVE_MAX_BARS"] = "10"
        self.assertEqual(load_live_trader_config_from_env().max_bars, 10)


if __name__ == "__main__":
    unittest.main()

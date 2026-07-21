"""
Unit tests for apps.paper_trader.main and apps.paper_trader.config.

Covers: the default memory/synthetic-bar path needs no credentials,
capital/risk-appetite overrides actually change bucket selection, the
groww historical path requires credentials, and - the regression this
suite exists to pin down - an empty bucket results in a session that
processes zero bars rather than silently falling back to trading every
candidate (see build_paper_session's step 5 for why that was a real bug
during development).

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_paper_trader.py -v
"""
from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from apps.live_trader.config import MissingGrowwCredentialsError
from apps.paper_trader.config import PaperTraderConfig, load_paper_trader_config_from_env
from apps.paper_trader.main import build_paper_session, run_paper_session
from core.audit.sink import NullAuditSink
from core.broker.paper import PaperBroker

DEFAULT_CONFIG = PaperTraderConfig(
    market_data_provider="memory",
    intraday_capital_override=None,
    risk_appetite_override=None,
    historical_from_date="2026-01-01",
    historical_to_date="2026-01-31",
    historical_interval="1d",
)


class LoadPaperTraderConfigFromEnvTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=False)
    def test_defaults_to_memory_with_no_overrides(self) -> None:
        for var in (
            "PAPER_MARKET_DATA_PROVIDER",
            "PAPER_INTRADAY_CAPITAL",
            "PAPER_RISK_APPETITE",
        ):
            os.environ.pop(var, None)

        config = load_paper_trader_config_from_env()

        self.assertEqual(config.market_data_provider, "memory")
        self.assertIsNone(config.intraday_capital_override)
        self.assertIsNone(config.risk_appetite_override)
        self.assertFalse(config.requires_groww_credentials)

    @patch.dict(os.environ, {"PAPER_MARKET_DATA_PROVIDER": "groww"})
    def test_groww_provider_requires_credentials_flag_is_set(self) -> None:
        config = load_paper_trader_config_from_env()
        self.assertTrue(config.requires_groww_credentials)

    @patch.dict(os.environ, {"PAPER_MARKET_DATA_PROVIDER": "polygon"})
    def test_unsupported_provider_raises(self) -> None:
        with self.assertRaises(ValueError):
            load_paper_trader_config_from_env()

    @patch.dict(os.environ, {"PAPER_RISK_APPETITE": "yolo"})
    def test_unsupported_risk_appetite_raises(self) -> None:
        with self.assertRaises(ValueError):
            load_paper_trader_config_from_env()

    @patch.dict(os.environ, {"PAPER_INTRADAY_CAPITAL": "not-a-number"})
    def test_non_numeric_capital_override_raises(self) -> None:
        with self.assertRaises(ValueError):
            load_paper_trader_config_from_env()


class BuildPaperSessionDefaultTests(unittest.TestCase):
    def test_default_config_needs_no_credentials_and_builds_a_paper_broker(self) -> None:
        for var in ("GROWW_API_KEY", "GROWW_ACCESS_TOKEN"):
            os.environ.pop(var, None)

        ctx = build_paper_session(config=DEFAULT_CONFIG)

        self.assertIsInstance(ctx.broker, PaperBroker)
        self.assertEqual(ctx.mandate.risk_appetite, "moderate")
        self.assertEqual(len(ctx.candidate_instruments), 8)  # full NSE universe

    def test_bucket_is_a_subset_of_candidates_and_capped_by_max_open_positions(self) -> None:
        ctx = build_paper_session(config=DEFAULT_CONFIG)

        candidate_symbols = {i.symbol for i in ctx.candidate_instruments}
        self.assertTrue(set(ctx.session_symbols).issubset(candidate_symbols))
        self.assertLessEqual(len(ctx.session_symbols), ctx.allocation_plan.max_open_positions)


class CapitalOverrideTests(unittest.TestCase):
    def test_very_low_capital_excludes_every_candidate_as_unaffordable(self) -> None:
        config = PaperTraderConfig(
            market_data_provider="memory",
            intraday_capital_override=100.0,  # far below any synthetic demo price
            risk_appetite_override="aggressive",  # isolate the affordability filter
            historical_from_date="2026-01-01",
            historical_to_date="2026-01-31",
            historical_interval="1d",
        )

        ctx = build_paper_session(config=config)

        self.assertEqual(ctx.session_symbols, [])
        reasons = {e.reason for e in ctx.bucket_result.excluded}
        self.assertEqual(reasons, {"unaffordable_at_current_capital"})

    def test_capital_override_does_not_mutate_the_mandate_file_on_disk(self) -> None:
        config = PaperTraderConfig(
            market_data_provider="memory",
            intraday_capital_override=999999.0,
            risk_appetite_override=None,
            historical_from_date="2026-01-01",
            historical_to_date="2026-01-31",
            historical_interval="1d",
        )

        build_paper_session(config=config)
        ctx_again = build_paper_session(config=DEFAULT_CONFIG)

        self.assertEqual(ctx_again.allocation_plan.intraday_capital, 10000.0)


class RiskAppetiteOverrideTests(unittest.TestCase):
    def test_conservative_selects_no_more_than_moderate(self) -> None:
        conservative_config = PaperTraderConfig(
            market_data_provider="memory",
            intraday_capital_override=None,
            risk_appetite_override="conservative",
            historical_from_date="2026-01-01",
            historical_to_date="2026-01-31",
            historical_interval="1d",
        )
        moderate_ctx = build_paper_session(config=DEFAULT_CONFIG)
        conservative_ctx = build_paper_session(config=conservative_config)

        self.assertLessEqual(len(conservative_ctx.session_symbols), len(moderate_ctx.session_symbols))

    def test_aggressive_selects_at_least_as_many_as_moderate(self) -> None:
        aggressive_config = PaperTraderConfig(
            market_data_provider="memory",
            intraday_capital_override=None,
            risk_appetite_override="aggressive",
            historical_from_date="2026-01-01",
            historical_to_date="2026-01-31",
            historical_interval="1d",
        )
        moderate_ctx = build_paper_session(config=DEFAULT_CONFIG)
        aggressive_ctx = build_paper_session(config=aggressive_config)

        self.assertGreaterEqual(len(aggressive_ctx.session_symbols), len(moderate_ctx.session_symbols))


class EmptyBucketRegressionTests(unittest.TestCase):
    """
    Pins down the bug found during development: an empty bucket must
    result in a session that streams and processes zero bars, not one
    that silently falls back to every candidate because none of them
    were ever explicitly subscribed.
    """

    def test_empty_bucket_processes_zero_bars_and_places_zero_orders(self) -> None:
        config = PaperTraderConfig(
            market_data_provider="memory",
            intraday_capital_override=1.0,  # nothing is affordable -> empty bucket
            risk_appetite_override="aggressive",
            historical_from_date="2026-01-01",
            historical_to_date="2026-01-31",
            historical_interval="1d",
        )
        ctx = build_paper_session(config=config)
        self.assertEqual(ctx.session_symbols, [])

        result = ctx.orchestrator.run_session(ctx.session_config)

        self.assertEqual(result.processed_bars, 0)
        self.assertEqual(result.placed_orders, [])


class GrowwProviderCredentialTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=False)
    def test_groww_provider_without_credentials_raises(self) -> None:
        for var in ("GROWW_API_KEY", "GROWW_ACCESS_TOKEN"):
            os.environ.pop(var, None)

        config = PaperTraderConfig(
            market_data_provider="groww",
            intraday_capital_override=None,
            risk_appetite_override=None,
            historical_from_date="2026-01-01",
            historical_to_date="2026-01-31",
            historical_interval="1d",
        )

        with self.assertRaises(MissingGrowwCredentialsError):
            build_paper_session(config=config)


class RunPaperSessionBannerTests(unittest.TestCase):
    def test_banner_and_summary_contain_key_fields(self) -> None:
        with patch("apps.paper_trader.main.load_paper_trader_config_from_env", return_value=DEFAULT_CONFIG):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                run_paper_session(audit_sink=NullAuditSink())
        output = buffer.getvalue()

        self.assertIn("=== PAPER TRADER START ===", output)
        self.assertIn("Risk appetite: moderate", output)
        self.assertIn("=== BUCKET SELECTION ===", output)
        self.assertIn("Candidates considered (8)", output)
        self.assertIn("=== SESSION SUMMARY ===", output)
        self.assertIn("=== PAPER TRADER END ===", output)


if __name__ == "__main__":
    unittest.main()

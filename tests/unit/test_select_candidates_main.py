"""
Unit tests for apps.live_trader.select_candidates_main.

The "memory" market-data provider path needs no credentials/network at
all (synthetic demo bars - see apps/live_trader/candidate_selection.py's
build_session_bars), so most of this exercises the real
select_session_instruments/save_candidate_selection pipeline end to end
without mocking a broker.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_select_candidates_main.py -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.live_trader.select_candidates_main import select_and_save_candidates
from core.market_data.candidate_selection_store import load_candidate_selection
from core.market_data.candidate_selection_store import save_candidate_selection as _real_save_candidate_selection

_MANDATE_YAML = """\
trading_date: "2026-07-24"

buckets:
  reserve_bucket: 10000.0
  intraday_bucket: 10000.0

risk_limits:
  max_daily_loss: 1000.0
  max_trades_per_day: 50
  stop_new_entries_after: "14:45"
  force_square_off_at: "15:15"

execution_mode: "paper"
# aggressive = no volatility ceiling (see core/market_data/bucket_selector.py) -
# the synthetic memory-provider demo bars are a dramatic breakout by
# design and would otherwise trip "moderate"'s ceiling, which isn't what
# this test is checking.
risk_appetite: "aggressive"
"""

_UNIVERSE_YAML = """\
universe_name: overnight_test
instruments:
  - symbol: RELIANCE
    exchange: NSE
    segment: CASH
    enabled: true
  - symbol: TCS
    exchange: NSE
    segment: CASH
    enabled: true
"""


class RecordingAlertSink:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send(self, message: str, *, severity: str = "error") -> None:
        self.messages.append((message, severity))


class SelectAndSaveCandidatesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = Path(tmp_dir.name)

        self.mandate_path = self.tmp_path / "mandate.yaml"
        self.mandate_path.write_text(_MANDATE_YAML, encoding="utf-8")

        self.universe_path = self.tmp_path / "universe.yaml"
        self.universe_path.write_text(_UNIVERSE_YAML, encoding="utf-8")

        self.selection_dir = self.tmp_path / "candidate_selection"

        env_patch = patch.dict(
            os.environ,
            {"LIVE_BROKER_PROVIDER": "paper", "LIVE_MARKET_DATA_PROVIDER": "memory"},
            clear=False,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_memory_provider_selects_and_persists_without_any_credentials(self) -> None:
        def _save(trading_date, instruments, **detail_kwargs):
            return _real_save_candidate_selection(trading_date, instruments, self.selection_dir, **detail_kwargs)

        with patch("apps.live_trader.select_candidates_main.save_candidate_selection", side_effect=_save):
            select_and_save_candidates(mandate_path=str(self.mandate_path), universe_path=str(self.universe_path))

        # Written to a strategy-suffixed file, not the legacy shared one -
        # selection is now strategy-aware (see
        # core/market_data/candidate_profile.py) and the default strategy
        # config (no LIVE_STRATEGY_CONFIG_PATH override in this test) is
        # simple_breakout_momentum.
        loaded = load_candidate_selection(
            "2026-07-24", self.selection_dir, strategy_name="simple_breakout_momentum"
        )
        self.assertIsNotNone(loaded)
        self.assertEqual({i.symbol for i in loaded.instruments}, {"RELIANCE", "TCS"})

        # "why" was captured, not just the bare symbol list - the memory
        # provider never runs prescreen, so news fields are None/empty.
        self.assertEqual({row["symbol"] for row in loaded.selected_detail}, {"RELIANCE", "TCS"})
        reliance_detail = next(row for row in loaded.selected_detail if row["symbol"] == "RELIANCE")
        self.assertIn("last_close", reliance_detail)
        self.assertIsNone(reliance_detail["news_score"])
        self.assertEqual(loaded.excluded_detail, [])  # only 2 candidates, both selected

    def test_failure_sends_alert_and_still_raises(self) -> None:
        alerts = RecordingAlertSink()

        with self.assertRaises(FileNotFoundError):
            select_and_save_candidates(
                mandate_path=str(self.mandate_path),
                universe_path=str(self.tmp_path / "does_not_exist.yaml"),
                alert_sink=alerts,
            )

        self.assertEqual(len(alerts.messages), 1)
        message, severity = alerts.messages[0]
        self.assertEqual(severity, "critical")
        self.assertIn("overnight candidate selection failed", message)


if __name__ == "__main__":
    unittest.main()

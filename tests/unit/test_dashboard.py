"""Tests for apps.reporting.dashboard: render_html and the live HTTP server."""
from __future__ import annotations

import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from apps.reporting import dashboard
from apps.reporting.dashboard import (
    AUTH_TOKEN_ENV_VAR,
    DashboardAuthNotConfiguredError,
    _DashboardHandler,
    _resolve_serve_host,
    render_html,
)


class RenderHtmlTests(unittest.TestCase):
    def test_no_sessions_yet_still_renders(self) -> None:
        html = render_html(None)
        self.assertIn("Trading Dashboard", html)
        self.assertIn("RELIANCE", html)  # full universe always shown

    def test_renders_a_portfolio_backtest_tab(self) -> None:
        html = render_html(None)
        self.assertIn("Portfolio Backtest", html)
        self.assertIn('id="tab-panel-backtest"', html)

    def test_error_message_gets_error_css_class(self) -> None:
        html = render_html(None, message="something went wrong", message_severity="error")
        self.assertIn('class="msg msg-error"', html)
        self.assertIn("something went wrong", html)

    def test_info_message_gets_info_css_class(self) -> None:
        html = render_html(None, message="all good", message_severity="info")
        self.assertIn('class="msg msg-info"', html)


class CandidateSelectionSectionTests(unittest.TestCase):
    """render_html's "Candidate selection" section, sourced from data/candidate_selection/ (see
    core/market_data/candidate_selection_store.py)."""

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.selection_dir = Path(tmp_dir.name)

        self._original_dir = dashboard.CANDIDATE_SELECTION_DIR
        dashboard.CANDIDATE_SELECTION_DIR = self.selection_dir
        self.addCleanup(setattr, dashboard, "CANDIDATE_SELECTION_DIR", self._original_dir)

    def test_no_selection_file_shows_fallback_message(self) -> None:
        html = render_html(None)
        self.assertIn("No overnight candidate selection saved yet", html)

    def test_selected_ticker_shows_price_volatility_and_news_reasoning(self) -> None:
        from core.market_data.candidate_selection_store import save_candidate_selection
        from core.market_data.universe_models import Instrument

        save_candidate_selection(
            "2026-07-24",
            [Instrument(symbol="RELIANCE", exchange="NSE")],
            self.selection_dir,
            selected_detail=[
                {
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "last_close": 1280.9,
                    "volatility_pct": 0.018,
                    "volatility_ceiling": 0.04,
                    "volatility_source": "historical",
                    "news_score": 4,
                    "headlines": ["Reliance surges on record profit growth"],
                }
            ],
            excluded_detail=[
                {"symbol": "INFY", "exchange": "NSE", "stage": "prescreen", "reason": "unaffordable", "ltp": 50000.0}
            ],
        )

        html = render_html(None)

        self.assertIn("2026-07-24", html)
        self.assertIn("NSE:RELIANCE", html)
        self.assertIn("1,280.90", html)
        self.assertIn("1.80%", html)
        self.assertIn("Reliance surges on record profit growth", html)
        self.assertIn("NSE:INFY", html)
        self.assertIn("unaffordable", html)

    def test_legacy_file_without_detail_falls_back_to_bare_symbol_list(self) -> None:
        """A selection file saved before selected_detail/excluded_detail existed (or any future schema
        change) - the count line must not contradict an empty-looking table."""
        from core.market_data.candidate_selection_store import save_candidate_selection
        from core.market_data.universe_models import Instrument

        save_candidate_selection("2026-07-24", [Instrument(symbol="AAATECH", exchange="NSE")], self.selection_dir)

        html = render_html(None)

        self.assertIn("NSE:AAATECH", html)
        self.assertIn("no detail saved for this selection", html)
        self.assertNotIn("none selected", html)

    def test_latest_file_by_date_is_used_when_multiple_exist(self) -> None:
        from core.market_data.candidate_selection_store import save_candidate_selection
        from core.market_data.universe_models import Instrument

        save_candidate_selection(
            "2026-07-23", [Instrument(symbol="OLDPICK", exchange="NSE")], self.selection_dir,
            selected_detail=[{"symbol": "OLDPICK", "exchange": "NSE", "last_close": 100.0}],
        )
        save_candidate_selection(
            "2026-07-24", [Instrument(symbol="NEWPICK", exchange="NSE")], self.selection_dir,
            selected_detail=[{"symbol": "NEWPICK", "exchange": "NSE", "last_close": 200.0}],
        )

        html = render_html(None)

        self.assertIn("NSE:NEWPICK", html)
        self.assertNotIn("NSE:OLDPICK", html)


def _write_session(
    sessions_dir: Path, session_id: str, *, broker_provider: str | None, app_name: str = "live_trader"
) -> Path:
    """
    A minimal session.json - ``broker_provider=None`` mirrors a session
    that died at an early safety gate, before build_live_session() ever
    got as far as choosing a broker (see run_live_session()'s
    exception-path metadata, which is just as bare). ``app_name`` lets a
    test build a kill_switch session - it sets ``broker_provider`` too,
    but isn't a trading session this dashboard should land on.
    """
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True)
    metadata = {"session_id": session_id, "app_name": app_name}
    if broker_provider is not None:
        metadata["broker_provider"] = broker_provider
    (session_dir / "session.json").write_text(
        json.dumps({"session_id": session_id, "status": "failed", "metadata": metadata}), encoding="utf-8"
    )
    return session_dir


def _minimal_backtest_result() -> dict:
    metrics = {
        "start_date": "2026-01-05",
        "end_date": "2026-01-06",
        "num_observations": 2,
        "start_value": 100.0,
        "end_value": 105.0,
        "total_return_pct": 5.0,
        "cagr_pct": 5.0,
        "annualized_volatility_pct": 1.0,
        "sharpe_ratio": 1.5,
        "max_drawdown_pct": 0.5,
    }
    report = {
        "trading_days": 2,
        "total_trades": 1,
        "winning_trades": 1,
        "losing_trades": 0,
        "breakeven_trades": 0,
        "total_realized_pnl": 500.0,
        "total_costs": 10.0,
        "win_rate": 1.0,
        "average_pnl_per_trade": 500.0,
        "average_win": 500.0,
        "average_loss": 0.0,
        "profit_factor": None,
        "max_drawdown": 0.0,
        "duplicate_orders_blocked": 0,
        "exit_reason_counts": {},
        "closed_trades": [],
    }
    return {
        "generated_at": "2026-01-06T00:00:00+00:00",
        "data_source": "csv",
        "window": {"from": "2026-01-05", "to": "2026-01-06"},
        "starting_capital": 100.0,
        "strategies": ["test_strategy"],
        "per_strategy": {
            "test_strategy": {"symbols": ["RELIANCE"], "intraday_capital": 50.0, "report": report}
        },
        "portfolio": {
            "report": report,
            "equity_curve": [["2026-01-05", 100.0], ["2026-01-06", 105.0]],
            "metrics": metrics,
        },
        "benchmark": {
            "symbol": "NIFTY",
            "series": [["2026-01-05", 24000.0], ["2026-01-06", 24100.0]],
            "metrics": {**metrics, "total_return_pct": 0.4},
        },
    }


class PortfolioBacktestTabTests(unittest.TestCase):
    """render_html's Portfolio Backtest tab, sourced from
    data/backtests/portfolio_backtest_latest.json (see
    apps/backtester/run_portfolio_backtest.py)."""

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self._original_path = dashboard.BACKTEST_RESULTS_PATH
        dashboard.BACKTEST_RESULTS_PATH = Path(tmp_dir.name) / "portfolio_backtest_latest.json"
        self.addCleanup(setattr, dashboard, "BACKTEST_RESULTS_PATH", self._original_path)

    def test_no_backtest_file_shows_fallback_message(self) -> None:
        html = render_html(None)
        self.assertIn("No portfolio backtest has been run yet", html)

    def test_backtest_result_renders_strategy_and_benchmark_figures(self) -> None:
        dashboard.BACKTEST_RESULTS_PATH.write_text(json.dumps(_minimal_backtest_result()), encoding="utf-8")

        html = render_html(None)

        self.assertIn("test_strategy", html)
        self.assertIn("NIFTY 50", html)
        self.assertIn("+5.00%", html)  # portfolio total return
        self.assertIn("+0.40%", html)  # benchmark total return

    def test_corrupt_file_falls_back_to_no_backtest_message(self) -> None:
        dashboard.BACKTEST_RESULTS_PATH.write_text("not valid json", encoding="utf-8")

        html = render_html(None)

        self.assertIn("No portfolio backtest has been run yet", html)


class LatestSessionDirTests(unittest.TestCase):
    """
    _latest_session_dir() must skip a session that failed before ever
    choosing a broker (e.g. an UncleanPriorSessionError refusal) in
    favor of the most recent session that actually got underway, even
    if the failed one has a later session_id (started more recently but
    finished in milliseconds) - see that function's own docstring.
    """

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.sessions_dir = Path(tmp_dir.name)

        self._original_sessions_dir = dashboard.SESSIONS_DIR
        dashboard.SESSIONS_DIR = self.sessions_dir
        self.addCleanup(setattr, dashboard, "SESSIONS_DIR", self._original_sessions_dir)

    def test_skips_a_later_instant_failure_in_favor_of_the_real_session(self) -> None:
        _write_session(self.sessions_dir, "20260724T073742_real", broker_provider="paper")
        _write_session(self.sessions_dir, "20260724T075003_diagnostic_failure", broker_provider=None)

        latest = dashboard._latest_session_dir()

        self.assertEqual(latest.name, "20260724T073742_real")

    def test_skips_a_later_kill_switch_run_even_though_it_sets_broker_provider(self) -> None:
        # kill_switch also sets broker_provider (it builds a real broker to flatten positions),
        # so the app_name check specifically is what must exclude it here - not just "has a broker".
        _write_session(self.sessions_dir, "20260724T073742_real", broker_provider="paper", app_name="live_trader")
        _write_session(self.sessions_dir, "20260724T100902_kill_switch", broker_provider="paper", app_name="kill_switch")

        latest = dashboard._latest_session_dir()

        self.assertEqual(latest.name, "20260724T073742_real")

    def test_falls_back_to_true_latest_when_every_session_failed_early(self) -> None:
        _write_session(self.sessions_dir, "20260724T070000_a", broker_provider=None)
        _write_session(self.sessions_dir, "20260724T080000_b", broker_provider=None)

        latest = dashboard._latest_session_dir()

        self.assertEqual(latest.name, "20260724T080000_b")

    def test_no_sessions_returns_none(self) -> None:
        self.assertIsNone(dashboard._latest_session_dir())


class DashboardServerIntegrationTests(unittest.TestCase):
    """
    Exercises the real HTTP server end to end (real socket, real request
    parsing, real file writes) against an isolated mandate file - the
    same thing manually verified via curl during development, kept as a
    durable regression test.
    """

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.mandate_path = Path(tmp_dir.name) / "mandate.yaml"
        self.mandate_path.write_text(
            'trading_date: "2026-07-07"\n\n'
            "buckets:\n  reserve_bucket: 40000\n  intraday_bucket: 60000\n\n"
            "risk_limits:\n  max_daily_loss: 2500\n  max_trades_per_day: 6\n"
            '  stop_new_entries_after: "14:45"\n  force_square_off_at: "15:20"\n\n'
            'execution_mode: "paper"\n\nrisk_appetite: "moderate"\n',
            encoding="utf-8",
        )
        self._original_mandate_path = dashboard.MANDATE_PATH
        dashboard.MANDATE_PATH = str(self.mandate_path)
        self.addCleanup(setattr, dashboard, "MANDATE_PATH", self._original_mandate_path)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _DashboardHandler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _post(self, path: str, data: dict) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = urllib.parse.urlencode(data)
        conn.request("POST", path, body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        location = resp.getheader("Location", "")
        resp.read()
        conn.close()
        return resp.status, location

    def test_valid_update_writes_mandate_and_redirects_with_success(self) -> None:
        status, location = self._post(
            "/update-mandate",
            {
                "intraday_bucket": "75000",
                "reserve_bucket": "40000",
                "max_daily_loss": "3000",
                "max_trades_per_day": "8",
                "stop_new_entries_after": "14:45",
                "force_square_off_at": "15:20",
                "risk_appetite": "aggressive",
            },
        )

        self.assertEqual(status, 303)
        self.assertIn("severity=info", location)
        self.assertIn("intraday_bucket: 75000", self.mandate_path.read_text())
        self.assertIn('risk_appetite: "aggressive"', self.mandate_path.read_text())

    def test_negative_capital_rejected_without_writing(self) -> None:
        original = self.mandate_path.read_text()

        status, location = self._post(
            "/update-mandate",
            {
                "intraday_bucket": "-500",
                "reserve_bucket": "40000",
                "max_daily_loss": "2500",
                "max_trades_per_day": "6",
                "stop_new_entries_after": "14:45",
                "force_square_off_at": "15:20",
                "risk_appetite": "moderate",
            },
        )

        self.assertEqual(status, 303)
        self.assertIn("severity=error", location)
        self.assertEqual(self.mandate_path.read_text(), original)

    def test_bad_time_format_rejected(self) -> None:
        status, location = self._post(
            "/update-mandate",
            {
                "intraday_bucket": "60000",
                "reserve_bucket": "40000",
                "max_daily_loss": "2500",
                "max_trades_per_day": "6",
                "stop_new_entries_after": "not-a-time",
                "force_square_off_at": "15:20",
                "risk_appetite": "moderate",
            },
        )

        self.assertEqual(status, 303)
        self.assertIn("severity=error", location)

    def test_bad_risk_appetite_rejected(self) -> None:
        status, location = self._post(
            "/update-mandate",
            {
                "intraday_bucket": "60000",
                "reserve_bucket": "40000",
                "max_daily_loss": "2500",
                "max_trades_per_day": "6",
                "stop_new_entries_after": "14:45",
                "force_square_off_at": "15:20",
                "risk_appetite": "yolo",
            },
        )

        self.assertEqual(status, 303)
        self.assertIn("severity=error", location)

    def test_unknown_post_path_returns_404(self) -> None:
        status, _ = self._post("/not-a-real-endpoint", {})
        self.assertEqual(status, 404)


class DashboardAuthTests(unittest.TestCase):
    """
    No token set -> every request is allowed through unchanged (today's
    loopback-trusted behavior). Token set -> every request needs it as an
    HTTP Basic password, and serve() itself refuses a non-loopback bind
    without one configured at all.
    """

    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _DashboardHandler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _get_status(self, *, password: str | None = None) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if password is not None:
            creds = base64.b64encode(f"anyuser:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
        conn.request("GET", "/", headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_no_token_configured_allows_unauthenticated_requests(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(AUTH_TOKEN_ENV_VAR, None)
            self.assertEqual(self._get_status(), 200)

    def test_token_configured_rejects_missing_credentials(self) -> None:
        with patch.dict(os.environ, {AUTH_TOKEN_ENV_VAR: "s3cret"}):
            self.assertEqual(self._get_status(), 401)

    def test_token_configured_rejects_wrong_password(self) -> None:
        with patch.dict(os.environ, {AUTH_TOKEN_ENV_VAR: "s3cret"}):
            self.assertEqual(self._get_status(password="wrong"), 401)

    def test_token_configured_accepts_correct_password(self) -> None:
        with patch.dict(os.environ, {AUTH_TOKEN_ENV_VAR: "s3cret"}):
            self.assertEqual(self._get_status(password="s3cret"), 200)

    def test_serve_refuses_non_loopback_host_without_token(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(AUTH_TOKEN_ENV_VAR, None)
            with self.assertRaises(DashboardAuthNotConfiguredError):
                _resolve_serve_host("localhost")

    def test_serve_allows_non_loopback_host_with_token(self) -> None:
        with patch.dict(os.environ, {AUTH_TOKEN_ENV_VAR: "s3cret"}):
            self.assertEqual(_resolve_serve_host("localhost"), "localhost")

    def test_serve_allows_loopback_host_without_token(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(AUTH_TOKEN_ENV_VAR, None)
            self.assertEqual(_resolve_serve_host("127.0.0.1"), "127.0.0.1")

    def test_serve_defaults_to_loopback_host_env_var(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(dashboard.HOST_ENV_VAR, None)
            os.environ.pop(AUTH_TOKEN_ENV_VAR, None)
            self.assertEqual(_resolve_serve_host(None), "127.0.0.1")


class ForecastSectionTests(unittest.TestCase):
    """apps.reporting.dashboard's Monte Carlo forecast section: degrades honestly, renders when data is available."""

    def _session_dir_with_symbols(self, symbols: list[str]) -> Path:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        session_dir = Path(tmp_dir.name)
        (session_dir / "session.json").write_text(
            json.dumps({"status": "completed", "metadata": {"subscribed_symbols": symbols}, "summary": None})
        )
        (session_dir / "events.jsonl").write_text("")
        return session_dir

    def test_no_sessions_yet_shows_unavailable(self) -> None:
        self.assertIn("forecast unavailable", render_html(None))

    def test_shows_unavailable_without_broker_credentials(self) -> None:
        session_dir = self._session_dir_with_symbols(["RELIANCE"])
        with patch.dict(os.environ, {}, clear=False):
            for var in ("GROWW_API_KEY", "GROWW_API_SECRET", "KITE_API_KEY", "KITE_ACCESS_TOKEN"):
                os.environ.pop(var, None)
            output = render_html(session_dir)
        self.assertIn("forecast unavailable", output)

    def test_renders_fan_chart_when_closes_available(self) -> None:
        session_dir = self._session_dir_with_symbols(["RELIANCE"])
        fake_closes = {"RELIANCE": [100.0 + i * 0.3 for i in range(30)]}
        with patch.object(dashboard, "_fetch_historical_closes", return_value=fake_closes):
            output = render_html(session_dir)
        self.assertIn("RELIANCE", output)
        self.assertIn("<svg", output)
        self.assertNotIn("forecast unavailable", output)

    def test_explicit_symbol_picker_overrides_traded_symbols(self) -> None:
        session_dir = self._session_dir_with_symbols(["RELIANCE"])
        fake_closes = {"WIPRO": [400.0 + i * 0.5 for i in range(30)]}
        with patch.object(dashboard, "_fetch_historical_closes", return_value=fake_closes) as fetch:
            output = render_html(session_dir, forecast_symbol="WIPRO")
        # only the picked symbol is requested, not the session's traded set
        fetch.assert_called_once()
        requested_symbols = [s for s, _exchange in fetch.call_args[0][0]]
        self.assertEqual(requested_symbols, ["WIPRO"])
        self.assertIn("WIPRO", output)

    def test_unknown_symbol_is_ignored_not_trusted(self) -> None:
        session_dir = self._session_dir_with_symbols(["RELIANCE"])
        fake_closes = {"RELIANCE": [100.0 + i * 0.3 for i in range(30)]}
        with patch.object(dashboard, "_fetch_historical_closes", return_value=fake_closes) as fetch:
            render_html(session_dir, forecast_symbol="<script>alert(1)</script>")
        requested_symbols = [s for s, _exchange in fetch.call_args[0][0]]
        self.assertEqual(requested_symbols, ["RELIANCE"])  # fell back to the session default, not the raw input


if __name__ == "__main__":
    unittest.main()

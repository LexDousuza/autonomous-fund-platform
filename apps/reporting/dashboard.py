"""
Trading dashboard: renders an HTML snapshot of the latest session (limits,
universe, positions, orders, live event log) from data/live_sessions/ - the
audit trail apps/paper_trader and apps/live_trader already write via
FileAuditSink.

Three modes:

    python -m apps.reporting.dashboard [output_path]              # static file, default: data/dashboard.html
    python -m apps.reporting.dashboard --serve [port]              # live HTTP server, default port 8765
    python -m apps.reporting.dashboard --obsidian <vault> [secs]   # Markdown sync into an Obsidian vault

The live server re-renders on every request and exposes a form to add a
stock to the trading universe (POST /add-symbol), persisted straight to
config/instruments/india_cash_universe.yaml. The Obsidian mode
(apps/reporting/obsidian_export.py) is a separate, plainer Markdown
renderer for viewing inside Obsidian - see that module's docstring for
what's different about it.

By default this binds to 127.0.0.1 only, unauthenticated - fine for
localhost or an SSH-tunneled view (see docs/runbooks/daily_paper_trading.md).
To reach it from anywhere else (e.g. your phone, via a tunnel like
Cloudflare Tunnel in front of it), set DASHBOARD_AUTH_TOKEN to a random
secret first - every request then needs it as an HTTP Basic auth password
(any username). Set DASHBOARD_HOST=0.0.0.0 (or pass a host to serve())
to actually bind beyond loopback; doing so without a token set is refused
outright, since /update-mandate and /add-symbol change live capital/risk
settings and are otherwise unauthenticated.
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from apps.live_trader.config import (
    MissingGrowwCredentialsError,
    MissingKiteCredentialsError,
    load_groww_credentials_from_env,
    load_kite_credentials_from_env,
)
from apps.reporting.charts import (
    comparison_line_chart,
    cumulative_pnl_line_chart,
    magnitude_bar_chart,
    monte_carlo_fan_chart,
    sparkline,
    trade_pnl_bar_chart,
)
from core.allocations.daily_mandate import (
    RISK_APPETITES,
    load_daily_mandate,
    update_daily_mandate_fields,
)
from core.analytics.session_performance import (
    ClosedTrade,
    compute_closed_trades,
    compute_cumulative_pnl_series,
    compute_portfolio_metrics,
)
from core.market_data.candidate_selection_store import CandidateSelection, load_candidate_selection
from core.market_data.errors import MarketDataError
from core.market_data.factory import build_market_data_feed
from core.market_data.factory_models import MarketDataConfig
from core.market_data.universe_loader import add_instrument_to_universe, load_instrument_universe
from core.pricing import monte_carlo

SESSIONS_DIR = Path("data/live_sessions")
CANDIDATE_SELECTION_DIR = Path("data/candidate_selection")
UNIVERSE_PATH = "config/instruments/india_cash_universe.yaml"
MANDATE_PATH = "config/mandates/sample_mandate.yaml"
BACKTEST_RESULTS_PATH = Path("data/backtests/portfolio_backtest_latest.json")
_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_FORECAST_HORIZON_DAYS = 10
_FORECAST_HISTORY_CALENDAR_DAYS = 60  # window of daily closes used to estimate drift/vol

AUTH_TOKEN_ENV_VAR = "DASHBOARD_AUTH_TOKEN"
HOST_ENV_VAR = "DASHBOARD_HOST"
LOOPBACK_HOST = "127.0.0.1"


class DashboardAuthNotConfiguredError(RuntimeError):
    """Raised when asked to bind somewhere other than loopback with no auth token set."""


def _configured_auth_token() -> str | None:
    return os.environ.get(AUTH_TOKEN_ENV_VAR) or None


def _is_authorized(handler: BaseHTTPRequestHandler) -> bool:
    """
    No token configured -> today's behavior unchanged (trusted, loopback-only
    access). Token configured -> every request needs HTTP Basic auth with
    that token as the password (username is ignored - this is a single
    shared secret, not a multi-user login).
    """
    token = _configured_auth_token()
    if token is None:
        return True

    header = handler.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.removeprefix("Basic ")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    _, _, password = decoded.partition(":")
    return hmac.compare_digest(password, token)


def _all_session_dirs() -> list[Path]:
    """All session directories, oldest first (matches the sortable session-id naming)."""
    if not SESSIONS_DIR.exists():
        return []
    return sorted((d for d in SESSIONS_DIR.iterdir() if d.is_dir()), key=lambda p: p.name)


_DASHBOARD_APP_NAMES = {"live_trader", "paper_trader"}


def _started_for_real(session_dir: Path) -> bool:
    """
    True for a real trading session (live_trader/paper_trader) that got
    at least as far as build_live_session() choosing a broker - i.e.
    it's worth landing on by default. False for two different kinds of
    noise: a session that died at an earlier safety gate (an unclean-
    prior-session refusal, a bad provider combination) with nothing yet
    to show, and a kill_switch run - a real ``broker_provider`` too, but
    an emergency-flatten utility, not a trading session this dashboard
    is meant to represent.
    """
    try:
        meta = json.loads((session_dir / "session.json").read_text()).get("metadata") or {}
    except (OSError, json.JSONDecodeError):
        return False
    return meta.get("app_name") in _DASHBOARD_APP_NAMES and bool(meta.get("broker_provider"))


def _latest_session_dir() -> Path | None:
    """
    Most recent session that actually got underway, not just the most
    recently *started* one - a session that fails within milliseconds
    at an early safety gate (e.g. a diagnostic/test invocation refused
    for an unclean prior session) can easily have a later session_id
    than a real session that ran for hours afterward, since the id is
    fixed at start time. Landing on that instant-failure by default is
    confusing, not informative. Falls back to the true latest if every
    session on record failed that early.
    """
    dirs = _all_session_dirs()
    for session_dir in reversed(dirs):
        if _started_for_real(session_dir):
            return session_dir
    return dirs[-1] if dirs else None


def _find_session_dir(session_id: str) -> Path | None:
    candidate = SESSIONS_DIR / session_id
    return candidate if candidate.is_dir() else None


def _load_events(session_dir: Path) -> list[dict]:
    events_path = session_dir / "events.jsonl"
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]


def _all_closed_trades() -> list[ClosedTrade]:
    """
    Every closed trade across every session on record, in chronological
    order (``_all_session_dirs`` is oldest-first, and each session's own
    events are already exit-ordered) - the sample ``compute_portfolio_metrics``
    needs to mean anything, since any one session's handful of trades is
    too small to compute a Sharpe ratio or drawdown from on its own.
    """
    trades: list[ClosedTrade] = []
    for session_dir in _all_session_dirs():
        trades.extend(compute_closed_trades(_load_events(session_dir)))
    return trades


def _session_label(session_dir: Path) -> str:
    """Human-readable dropdown label: '2026-07-13 08:18 · live_trader · completed'."""
    try:
        data = json.loads((session_dir / "session.json").read_text())
    except (OSError, json.JSONDecodeError):
        return session_dir.name

    app_name = (data.get("metadata") or {}).get("app_name", "?")
    status = data.get("status", "?")
    date_part = session_dir.name.split("_")[0]
    try:
        when = datetime.strptime(date_part, "%Y%m%dT%H%M%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        when = session_dir.name

    return f"{when} · {app_name} · {status}"


def _fetch_historical_closes(symbols_with_exchange: list[tuple[str, str]]) -> dict[str, list[float]] | None:
    """
    Batch-fetch a short window of real daily closes for every (symbol,
    exchange) pair, via whichever broker has credentials configured
    (Kite tried first - this platform's trading broker - Groww as
    fallback), reusing the existing historical-feed factory.

    Returns ``None`` if neither broker is configured, if the feed build
    or fetch fails, or if any instrument comes back with no bars -
    callers must then show a "forecast unavailable" note for the whole
    section rather than partially rendering it.
    """
    if not symbols_with_exchange:
        return {}

    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=_FORECAST_HISTORY_CALENDAR_DAYS)).isoformat()
    instruments = [{"symbol": symbol, "exchange": exchange} for symbol, exchange in symbols_with_exchange]

    try:
        credentials = load_kite_credentials_from_env()
        config = MarketDataConfig(
            provider="kite",
            mode="historical",
            metadata={
                "api_key": credentials.api_key,
                "access_token": credentials.access_token,
                "instruments": instruments,
                "from_date": from_date,
                "to_date": to_date,
            },
        )
    except MissingKiteCredentialsError:
        try:
            credentials = load_groww_credentials_from_env()
            config = MarketDataConfig(
                provider="groww",
                mode="historical",
                metadata={
                    "api_key": credentials.api_key,
                    "secret": credentials.secret,
                    "instruments": instruments,
                    "from_date": from_date,
                    "to_date": to_date,
                },
            )
        except MissingGrowwCredentialsError:
            return None

    try:
        feed = build_market_data_feed(config)
        closes_by_symbol: dict[str, list[float]] = {}
        for symbol, _exchange in symbols_with_exchange:
            feed.subscribe(symbol)
            closes = [bar.close for bar in feed.get_history(symbol)]
            if not closes:
                return None
            closes_by_symbol[symbol] = closes
        return closes_by_symbol
    except (MarketDataError, ValueError):
        return None


def _order_row(o: dict) -> str:
    return (
        f'<tr><td>{o.get("exchange")}:{o.get("symbol")}</td><td>{o.get("side")}</td>'
        f'<td>{o.get("quantity")}</td><td>{o.get("average_price")}</td><td>{o.get("status")}</td></tr>'
    )


_PAYLOAD_SUMMARY_MAX_CHARS = 80


def _event_row(e: dict) -> str:
    """
    Payload as a collapsed one-line summary that expands to pretty-printed
    JSON, not a raw dict dumped inline - a broker error's stack-trace-shaped
    message could otherwise blow out the row (and, unescaped, break the page).
    """
    payload = e.get("payload") or {}
    compact = json.dumps(payload)
    summary = html.escape(compact[:_PAYLOAD_SUMMARY_MAX_CHARS])
    if len(compact) > _PAYLOAD_SUMMARY_MAX_CHARS:
        summary += "…"
    pretty = html.escape(json.dumps(payload, indent=2))
    return (
        f'<tr><td>{html.escape(str(e.get("timestamp", "")))}</td>'
        f'<td>{html.escape(str(e.get("event_type")))}</td>'
        f'<td>{html.escape(str(e.get("symbol") or ""))}</td>'
        f'<td><details class="payload-cell"><summary>{summary or "{}"}</summary><pre>{pretty}</pre></details></td></tr>'
    )


def _latest_candidate_selection() -> CandidateSelection | None:
    """
    The most recently computed candidate selection (see
    core/market_data/candidate_selection_store.py) - by filename (each is
    named ``<trading_date>.json``, so lexical order is chronological),
    independent of which live-trading session the dashboard happens to
    be showing. ``None`` if the overnight job has never run.
    """
    if not CANDIDATE_SELECTION_DIR.exists():
        return None
    files = sorted(CANDIDATE_SELECTION_DIR.glob("*.json"))
    if not files:
        return None
    return load_candidate_selection(files[-1].stem, CANDIDATE_SELECTION_DIR)


def _fmt_pct(value: float | None) -> str:
    return f"{value * 100:.2f}%" if value is not None else "-"


def _fmt_price(value: float | None) -> str:
    return f"₹{value:,.2f}" if value is not None else "-"


def _fmt_ratio(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def _selected_candidate_row(row: dict) -> str:
    volatility = (
        f'{_fmt_pct(row.get("volatility_pct"))} vs {_fmt_pct(row.get("volatility_ceiling"))} ceiling '
        f'({html.escape(str(row.get("volatility_source") or ""))})'
        if row.get("volatility_pct") is not None
        else "-"
    )
    news_score = row.get("news_score")
    if news_score is None:
        news = '<span class="chart-empty">not news-checked (non-Angel One provider)</span>'
    else:
        headlines = row.get("headlines") or []
        headline_list = "".join(f"<li>{html.escape(h)}</li>" for h in headlines) or "<li>no headlines</li>"
        news = f'score {news_score} <details><summary>{len(headlines)} headline(s)</summary><ul>{headline_list}</ul></details>'
    return (
        f'<tr><td>{html.escape(row.get("exchange", ""))}:{html.escape(row.get("symbol", ""))}</td>'
        f'<td>{_fmt_price(row.get("last_close"))}</td><td>{volatility}</td><td>{news}</td></tr>'
    )


def _excluded_candidate_row(row: dict) -> str:
    reason = html.escape(str(row.get("reason") or ""))
    stage = html.escape(str(row.get("stage") or ""))
    detail_bits = []
    if row.get("ltp") is not None:
        detail_bits.append(f'LTP {_fmt_price(row["ltp"])}')
    if row.get("last_close") is not None:
        detail_bits.append(f'close {_fmt_price(row["last_close"])}')
    if row.get("volatility_pct") is not None:
        detail_bits.append(f'vol {_fmt_pct(row["volatility_pct"])} vs {_fmt_pct(row.get("volatility_ceiling"))}')
    if row.get("news_score") is not None:
        detail_bits.append(f'news score {row["news_score"]}')
    detail = ", ".join(detail_bits) or "-"
    return (
        f'<tr><td>{html.escape(row.get("exchange", ""))}:{html.escape(row.get("symbol", ""))}</td>'
        f'<td class="reason-badge reason-{stage}">{stage}: {reason}</td><td>{detail}</td></tr>'
    )


def _load_portfolio_backtest_result() -> dict | None:
    """
    Read the JSON ``apps/backtester/run_portfolio_backtest.py`` writes
    after a run - ``None`` if it hasn't been run yet, or the file is
    unreadable/corrupt (never crashes the dashboard over a stale or
    partially-written file).
    """
    if not BACKTEST_RESULTS_PATH.exists():
        return None
    try:
        return json.loads(BACKTEST_RESULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _rebase_to_pct_return(series: list[list]) -> list[tuple[str, float]]:
    """``[[date_iso, value], ...]`` -> ``[(date_iso, pct_change_from_first_value), ...]``."""
    if not series:
        return []
    start = series[0][1]
    return [(point[0], (point[1] / start - 1.0) * 100.0) for point in series]


def _fmt_signed_pct(value: float | None) -> str:
    return f"{value:+.2f}%" if value is not None else "n/a"


def _fmt_mag_pct(value: float | None) -> str:
    return f"{value:.2f}%" if value is not None else "n/a"


def _backtest_strategy_row(name: str, info: dict) -> str:
    report = info["report"]
    profit_factor = f"{report['profit_factor']:.2f}" if report["profit_factor"] is not None else "n/a"
    return (
        f"<tr><td>{html.escape(name)}</td>"
        f"<td>{html.escape(', '.join(info['symbols']) or 'none')}</td>"
        f"<td>{report['total_trades']}</td>"
        f"<td>{report['win_rate']:.1%}</td>"
        f"<td>{report['total_realized_pnl']:+,.2f}</td>"
        f"<td>{profit_factor}</td>"
        f"<td>{report['max_drawdown']:,.2f}</td></tr>"
    )


def _render_backtest_tab(result: dict | None) -> str:
    """
    Render the Portfolio Backtest tab's content from the JSON
    ``apps/backtester/run_portfolio_backtest.py`` persists - a read-only
    snapshot of the last run, not something this dashboard can trigger
    itself (that script can take minutes and touches no live trading
    state, so it stays a deliberate, separate manual/cron step).
    """
    if result is None:
        return (
            '<p class="chart-empty">No portfolio backtest has been run yet. Run '
            '<code>python -m apps.backtester.run_portfolio_backtest</code> to populate this tab.</p>'
        )

    portfolio, benchmark = result["portfolio"], result["benchmark"]
    p_metrics, b_metrics = portfolio["metrics"], benchmark["metrics"]
    combined_report = portfolio["report"]

    compare_rows = "".join(
        f"<tr><td>{label}</td><td>{portfolio_value}</td><td>{benchmark_value}</td></tr>"
        for label, portfolio_value, benchmark_value in [
            ("Total return", _fmt_signed_pct(p_metrics["total_return_pct"]), _fmt_signed_pct(b_metrics["total_return_pct"])),
            ("CAGR", _fmt_signed_pct(p_metrics["cagr_pct"]), _fmt_signed_pct(b_metrics["cagr_pct"])),
            (
                "Annualized volatility",
                _fmt_mag_pct(p_metrics["annualized_volatility_pct"]),
                _fmt_mag_pct(b_metrics["annualized_volatility_pct"]),
            ),
            (
                "Sharpe ratio",
                f"{p_metrics['sharpe_ratio']:.2f}" if p_metrics["sharpe_ratio"] is not None else "n/a",
                f"{b_metrics['sharpe_ratio']:.2f}" if b_metrics["sharpe_ratio"] is not None else "n/a",
            ),
            ("Max drawdown", _fmt_mag_pct(p_metrics["max_drawdown_pct"]), _fmt_mag_pct(b_metrics["max_drawdown_pct"])),
        ]
    )

    strategy_rows = "".join(
        _backtest_strategy_row(name, info) for name, info in result["per_strategy"].items()
    ) or '<tr><td colspan=7>no strategies</td></tr>'

    equity_pct = _rebase_to_pct_return(portfolio["equity_curve"])
    benchmark_pct = _rebase_to_pct_return(benchmark["series"])

    profit_factor = (
        f"{combined_report['profit_factor']:.2f}" if combined_report["profit_factor"] is not None else "n/a (no losses)"
    )

    return f"""
<p class="historical-banner">Research backtest only - historical mechanics, not a live/paper trading
recommendation. Generated {html.escape(result["generated_at"])} &middot; data source: {html.escape(result["data_source"])}
&middot; window {html.escape(result["window"]["from"])} to {html.escape(result["window"]["to"])} &middot;
starting capital &#8377;{result["starting_capital"]:,.0f} (a research figure, independent of
config/mandates/approved_capital.yaml's live-trading approval gate).</p>

<h2>Portfolio vs NIFTY 50 benchmark</h2>
<div class="charts-grid">
  <div class="chart-card" style="grid-column: 1 / -1;">
    <h3>Cumulative return (%)</h3>
    {comparison_line_chart(equity_pct, "Strategy portfolio", benchmark_pct, "NIFTY 50")}
  </div>
</div>
<table class="compare-table"><tr><th>Metric</th><th>Portfolio</th><th>NIFTY 50</th></tr>{compare_rows}</table>
<p class="mandate-hint">Volatility/Sharpe here are computed on the portfolio's full capital base
(mostly held in reserve, by design), not just capital actually deployed intraday - they read much
calmer than the benchmark's for that reason and aren't a claim the strategy is "safer." Total
return/CAGR are the directly comparable figures.</p>

<h2>Combined portfolio results ({combined_report["total_trades"]} closed trades)</h2>
<div class="stat-grid">
  <div class="stat-card"><div class="stat-label">Net P&amp;L (after costs)</div><div class="stat-value">&#8377;{combined_report["total_realized_pnl"]:+,.0f}</div></div>
  <div class="stat-card"><div class="stat-label">Total costs</div><div class="stat-value">&#8377;{combined_report["total_costs"]:,.0f}</div></div>
  <div class="stat-card"><div class="stat-label">Win rate</div><div class="stat-value">{combined_report["win_rate"]:.1%}</div></div>
  <div class="stat-card"><div class="stat-label">Profit factor</div><div class="stat-value">{profit_factor}</div></div>
  <div class="stat-card"><div class="stat-label">Max drawdown</div><div class="stat-value">&#8377;{combined_report["max_drawdown"]:,.0f}</div></div>
</div>

<h2>Per-strategy breakdown</h2>
<table><tr><th>Strategy</th><th>Symbols traded</th><th>Trades</th><th>Win rate</th><th>Net P&amp;L</th><th>Profit factor</th><th>Max drawdown</th></tr>{strategy_rows}</table>
"""


def render_html(
    session_dir: Path | None,
    message: str | None = None,
    message_severity: str = "info",
    forecast_symbol: str | None = None,
) -> str:
    universe = load_instrument_universe(UNIVERSE_PATH)
    instruments = universe.enabled_instruments()
    all_symbols = sorted({i.symbol for i in instruments})
    symbol_exchange = {i.symbol: i.exchange for i in instruments}
    # Only trust a symbol picked via the forecast dropdown if it's actually in
    # the universe - a query param is attacker-controlled input, and this
    # value goes straight into a broker API fetch.
    if forecast_symbol not in all_symbols:
        forecast_symbol = None

    try:
        mandate = load_daily_mandate(MANDATE_PATH)
    except (FileNotFoundError, ValueError):
        mandate = None

    backtest_tab_html = _render_backtest_tab(_load_portfolio_backtest_result())

    all_sessions = _all_session_dirs()
    latest_session_dir = all_sessions[-1] if all_sessions else None
    viewing_historical = session_dir is not None and session_dir != latest_session_dir
    recent_sessions = list(reversed(all_sessions[-30:]))  # newest first, capped at 30
    session_picker_options = "".join(
        f'<option value="{html.escape(d.name)}"{" selected" if d == session_dir else ""}>'
        f'{html.escape(_session_label(d))}</option>'
        for d in recent_sessions
    )

    if session_dir is None:
        session: dict = {"status": "no sessions yet", "metadata": {}, "summary": None}
        events: list[dict] = []
    else:
        session = json.loads((session_dir / "session.json").read_text())
        events = _load_events(session_dir)

    meta = session.get("metadata") or {}
    plan = meta.get("allocation_plan") or {}
    summary = session.get("summary")
    traded = set(meta.get("subscribed_symbols") or [])

    forecast_target_symbols = [forecast_symbol] if forecast_symbol else sorted(traded)
    forecast_symbols = [(s, symbol_exchange.get(s, "NSE")) for s in forecast_target_symbols]
    closes_by_symbol = _fetch_historical_closes(forecast_symbols)
    forecast_cards = []
    if closes_by_symbol:
        seed_date = date.today().isoformat()
        for symbol, closes in closes_by_symbol.items():
            try:
                mc_forecast = monte_carlo.forecast(
                    symbol, closes, _FORECAST_HORIZON_DAYS, seed=monte_carlo.stable_seed(symbol, seed_date)
                )
            except ValueError:
                continue  # not enough history for this one symbol - skip just its card
            forecast_cards.append(
                f'<div class="chart-card"><h3>{html.escape(symbol)}</h3>'
                f'{monte_carlo_fan_chart(closes, mc_forecast)}</div>'
            )
    forecast_section_body = (
        f'<div class="charts-grid">{"".join(forecast_cards)}</div>'
        if forecast_cards
        else '<p class="chart-empty">forecast unavailable (no broker credentials configured, or the fetch failed)</p>'
    )
    forecast_symbol_options = "".join(f'<option value="{html.escape(s)}">' for s in all_symbols)
    forecast_picker = f"""<form class="toolbar" method="get" action="/">
  <input type="hidden" name="session" value="{html.escape(session_dir.name) if session_dir else ""}">
  <label for="forecast-symbol-picker">Symbol</label>
  <input id="forecast-symbol-picker" name="symbol" list="forecast-symbol-options"
    placeholder="type a ticker, e.g. RELIANCE" value="{html.escape(forecast_symbol or "")}" autocomplete="off">
  <datalist id="forecast-symbol-options">{forecast_symbol_options}</datalist>
  <button type="submit">Show forecast</button>
  {'<a href="/?session=' + quote(session_dir.name if session_dir else "") + '" class="mandate-hint">reset to this session\'s symbols</a>' if forecast_symbol else ''}
</form>"""

    universe_rows = "".join(
        f'<tr><td>{html.escape(s)}</td>'
        f'<td>{"traded this session" if s in traded else "not selected"}</td></tr>'
        for s in all_symbols
    )

    candidate_selection = _latest_candidate_selection()
    if candidate_selection is None:
        candidate_selection_body = (
            '<p class="chart-empty">No overnight candidate selection saved yet - see '
            '<code>scripts/run_overnight_selection.sh</code> / '
            '<code>apps/live_trader/select_candidates_main.py</code>. '
            'Sessions fall back to selecting live in the meantime.</p>'
        )
    else:
        if candidate_selection.selected_detail:
            selected_rows = "".join(_selected_candidate_row(r) for r in candidate_selection.selected_detail)
        elif candidate_selection.instruments:
            # A file saved before this reasoning was added to the schema
            # (or any future schema change) - fall back to the bare
            # symbol list rather than showing "none selected" while the
            # summary line above says N were picked.
            selected_rows = "".join(
                f'<tr><td>{html.escape(i.exchange)}:{html.escape(i.symbol)}</td>'
                f'<td colspan=3 class="chart-empty">no detail saved for this selection</td></tr>'
                for i in candidate_selection.instruments
            )
        else:
            selected_rows = '<tr><td colspan=4>none selected</td></tr>'
        excluded_reason_counts = Counter(r.get("reason") for r in candidate_selection.excluded_detail)
        excluded_summary = ", ".join(
            f"{html.escape(str(reason))}: {count}"
            for reason, count in sorted(excluded_reason_counts.items(), key=lambda kv: -kv[1])
        ) or "none"
        excluded_rows = "".join(_excluded_candidate_row(r) for r in candidate_selection.excluded_detail) or (
            '<tr><td colspan=3>none excluded</td></tr>'
        )
        candidate_selection_body = f"""
<p class="session-info">Trading date: <b>{html.escape(candidate_selection.trading_date)}</b> ·
computed {html.escape(candidate_selection.computed_at)} ·
{len(candidate_selection.instruments)} selected, {len(candidate_selection.excluded_detail)} excluded</p>
<table><tr><th>Instrument</th><th>Last close</th><th>Volatility</th><th>News</th></tr>{selected_rows}</table>
<details>
<summary>Excluded ({len(candidate_selection.excluded_detail)}) - by reason: {excluded_summary}</summary>
<div class="toolbar"><input type="search" placeholder="Filter excluded..." oninput="filterTable(this,'excluded-table')"></div>
<table id="excluded-table"><tr><th>Instrument</th><th>Reason</th><th>Detail</th></tr>{excluded_rows}</table>
</details>
"""

    if summary:
        risk = summary.get("final_session_risk_state") or {}
        pnl = risk.get("realized_pnl", 0)
        entries_taken_count = risk.get("entries_taken", 0)
        positions = summary.get("final_positions", [])
        pos_rows = "".join(
            f'<tr><td>{p["exchange"]}:{p["symbol"]}</td><td>{p["quantity"]}</td>'
            f'<td>{p["average_price"]}</td></tr>'
            for p in positions
        ) or "<tr><td colspan=3>no positions</td></tr>"
        order_rows = "".join(
            _order_row(o) for o in summary.get("placed_orders", []) + summary.get("exit_orders", [])
        ) or "<tr><td colspan=5>none</td></tr>"
        funnel_items = [
            ("Generated", summary.get("generated_signals", 0)),
            ("Approved", summary.get("approved_signals", 0)),
            ("Rejected", summary.get("rejected_signals", 0)),
        ]
    else:
        pnl = "session in progress"
        entries_taken_count = sum(1 for e in events if e.get("event_type") == "order_placed")
        pos_rows = "<tr><td colspan=3>session still running - see live event log</td></tr>"
        order_rows = "<tr><td colspan=5>session still running - see live event log</td></tr>"
        funnel_items = [
            ("Generated", sum(1 for e in events if e.get("event_type") == "signal_generated")),
            ("Approved", sum(1 for e in events if e.get("event_type") == "signal_approved")),
            ("Rejected", sum(1 for e in events if e.get("event_type") == "signal_rejected")),
        ]

    max_trades = plan.get("max_trades_per_day")
    trades_used = f'{entries_taken_count} / {max_trades if max_trades is not None else "n/a"}'
    trades_meter_pct = min(100.0, entries_taken_count / max_trades * 100) if max_trades else 0.0

    closed_trades = compute_closed_trades(events)
    pnl_series = compute_cumulative_pnl_series(closed_trades)

    if isinstance(pnl, (int, float)):
        pnl_display = f"₹{pnl:+,.2f}"
        pnl_card_class = "hero-good" if pnl >= 0 else "hero-critical"
        pnl_spark = sparkline([v for _, v in pnl_series], color="currentColor")
    else:
        pnl_display = html.escape(str(pnl))
        pnl_card_class = "hero-neutral"
        pnl_spark = ""

    closed_trade_rows = "".join(
        f'<tr><td>{html.escape(t.symbol)}</td><td>{t.entry_price:.2f}</td><td>{t.exit_price:.2f}</td>'
        f'<td>{t.quantity}</td><td>{t.pnl:+,.2f}</td></tr>'
        for t in closed_trades
    ) or "<tr><td colspan=5>no closed trades yet</td></tr>"

    portfolio_metrics = compute_portfolio_metrics(_all_closed_trades())

    rejection_counts = Counter(
        (e.get("payload") or {}).get("reason") or (e.get("payload") or {}).get("rejection_stage") or "unknown"
        for e in events
        if e.get("event_type") == "signal_rejected"
    )
    rejection_items = sorted(rejection_counts.items(), key=lambda kv: -kv[1])

    event_rows = "".join(_event_row(e) for e in events[-50:]) or "<tr><td colspan=4>no events</td></tr>"

    message_html = (
        f'<p class="msg msg-{html.escape(message_severity)}">{html.escape(message)}</p>' if message else ""
    )

    return f"""<title>Trading Dashboard</title>
<meta name="color-scheme" content="light dark">
<style>
:root {{
  --viz-page: #f4f3f0; --viz-surface: #fcfcfb; --viz-text-primary: #0b0b0b; --viz-text-secondary: #52514e;
  --viz-grid: #e1e0d9; --viz-baseline: #c3c2b7; --viz-series-1: #2a78d6; --viz-series-2: #d68a2a;
  --viz-good: #0ca30c; --viz-critical: #d03b3b;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  padding: 24px 32px 56px; max-width: 1400px; margin: auto;
  background: var(--viz-page); color: var(--viz-text-primary); line-height: 1.5;
}}
h1 {{ font-size: 26px; font-weight: 700; margin: 0 0 20px; letter-spacing: -0.01em; }}
h2 {{ font-size: 16px; font-weight: 600; margin: 30px 0 12px; letter-spacing: -0.01em; }}
h2:first-of-type {{ margin-top: 28px; }}
h3 {{ font-size: 13px; font-weight: 600; color: var(--viz-text-secondary); margin: 0 0 12px; text-transform: uppercase; letter-spacing: 0.04em; }}
p {{ color: var(--viz-text-secondary); font-size: 14px; }}
.card {{ background: var(--viz-surface); border: 1px solid var(--viz-grid); border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.04); padding: 16px 18px; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; background: var(--viz-surface);
  border: 1px solid var(--viz-grid); border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.04); overflow: hidden; }}
th, td {{ border-bottom: 1px solid var(--viz-grid); padding: 7px 12px; text-align: left; font-size: 13.5px; }}
td {{ font-variant-numeric: tabular-nums; }}
tr:last-child td {{ border-bottom: none; }}
th {{ background: color-mix(in srgb, var(--viz-series-1) 6%, transparent); cursor: pointer; user-select: none;
  font-weight: 600; color: var(--viz-text-secondary); font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; }}
th:hover {{ background: color-mix(in srgb, var(--viz-series-1) 12%, transparent); }}
input, select, button {{
  font-family: inherit; font-size: 13.5px; padding: 8px 12px; border-radius: 7px;
  border: 1px solid var(--viz-grid); background: var(--viz-surface); color: var(--viz-text-primary);
}}
input:focus, select:focus {{ outline: 2px solid color-mix(in srgb, var(--viz-series-1) 40%, transparent); outline-offset: 1px; }}
button {{ background: var(--viz-series-1); color: #fff; border: none; font-weight: 600; cursor: pointer; }}
button:hover {{ filter: brightness(1.08); }}
label {{ font-size: 13px; color: var(--viz-text-secondary); }}
.field {{ display: flex; flex-direction: column; gap: 4px; }}
.field label {{ font-weight: 500; }}
.msg {{ padding: 10px 16px; border-radius: 8px; font-size: 13.5px; margin-bottom: 16px; }}
.msg-info {{ background: color-mix(in srgb, var(--viz-series-1) 12%, transparent); border: 1px solid var(--viz-series-1); color: var(--viz-text-primary); }}
.msg-error {{ background: color-mix(in srgb, var(--viz-critical) 12%, transparent); border: 1px solid var(--viz-critical); color: var(--viz-critical); }}
.toolbar {{ display: flex; gap: 12px; align-items: center; margin-bottom: 10px; }}
.toolbar input {{ flex: 1; max-width: 320px; }}
form.add-stock {{ display: flex; gap: 8px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }}
.top-bar {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between;
  margin-bottom: 20px; padding: 10px 16px; background: var(--viz-surface); border: 1px solid var(--viz-grid); border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.04); }}
.top-bar select {{ font-size: 13px; }}
.top-bar .right {{ display: flex; gap: 14px; align-items: center; }}
.top-bar label {{ display: flex; align-items: center; gap: 6px; font-size: 13px; }}
.market-status {{ font-size: 13px; font-weight: 500; padding: 6px 12px; border-radius: 7px; border: 1px solid var(--viz-grid); }}
.market-status.open {{ color: var(--viz-good); border-color: var(--viz-good); background: color-mix(in srgb, var(--viz-good) 8%, transparent); }}
.market-status.closed {{ color: var(--viz-text-secondary); }}
.historical-banner {{ background: color-mix(in srgb, var(--viz-series-1) 10%, transparent);
  border: 1px solid var(--viz-series-1); padding: 10px 16px; border-radius: 8px; font-size: 13.5px; margin-bottom: 16px; }}
.historical-banner a {{ color: var(--viz-series-1); font-weight: 600; }}
.session-info {{ margin: 0 0 8px; }}
.session-info b {{ color: var(--viz-text-primary); }}
.stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 20px; }}
.stat-card {{ background: var(--viz-surface); border: 1px solid var(--viz-grid); border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.04); padding: 12px 14px; }}
.stat-card .stat-label {{ font-size: 12px; color: var(--viz-text-secondary); text-transform: uppercase; letter-spacing: 0.03em; }}
.stat-card .stat-value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
.hero-card {{ background: var(--viz-surface); border: 1px solid var(--viz-grid); border-radius: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.04);
  padding: 20px 24px; margin-bottom: 14px; display: flex; align-items: center; justify-content: space-between; gap: 20px; }}
.hero-card .stat-label {{ font-size: 12px; color: var(--viz-text-secondary); text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 6px; }}
.hero-value {{ font-size: 48px; font-weight: 700; letter-spacing: -0.02em; line-height: 1; color: inherit; }}
.hero-good {{ color: var(--viz-good); }}
.hero-critical {{ color: var(--viz-critical); }}
.hero-neutral {{ color: var(--viz-text-secondary); }}
.hero-neutral .hero-value {{ font-size: 26px; font-weight: 600; }}
.viz-sparkline {{ display: block; flex-shrink: 0; }}
.charts-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 12px; margin-bottom: 20px; }}
.chart-card {{ background: var(--viz-surface); border: 1px solid var(--viz-grid); border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.04); padding: 16px 18px; }}
.chart-card h3 {{ margin: 0 0 12px; }}
.chart-empty {{ color: var(--viz-text-secondary); font-size: 13px; }}
.viz-chart {{ width: 100%; height: auto; display: block; overflow: visible; }}
.viz-baseline {{ stroke: var(--viz-baseline); stroke-width: 1; }}
.viz-grid {{ stroke: var(--viz-grid); stroke-width: 1; }}
.viz-axis {{ fill: var(--viz-text-secondary); font-size: 12px; font-variant-numeric: tabular-nums; }}
.viz-label {{ fill: var(--viz-text-primary); font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; }}
.viz-label-sm {{ fill: var(--viz-text-primary); font-size: 11px; font-weight: 600; font-variant-numeric: tabular-nums; }}
.viz-legend {{ font-size: 12px; color: var(--viz-text-secondary); margin: 8px 0 0; }}
.viz-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin: 0 4px 0 12px; }}
.viz-dot:first-child {{ margin-left: 0; }}
.chart-tooltip {{ position: fixed; pointer-events: none; background: var(--viz-surface); color: var(--viz-text-primary);
  border: 1px solid var(--viz-grid); padding: 4px 8px; border-radius: 4px; font-size: 12px; z-index: 100;
  box-shadow: 0 2px 8px rgba(0,0,0,0.15); }}
.chart-tooltip[hidden] {{ display: none; }}
.meter-track {{ background: color-mix(in srgb, var(--viz-series-1) 18%, transparent); border-radius: 4px; height: 6px; margin-top: 8px; overflow: hidden; }}
.meter-fill {{ background: var(--viz-series-1); height: 100%; }}
.mandate-form {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; margin-bottom: 10px; }}
.mandate-form .actions {{ grid-column: 1 / -1; display: flex; align-items: center; gap: 12px; }}
.mandate-hint {{ font-size: 12.5px; color: var(--viz-text-secondary); margin: 8px 0 0; }}
details {{ background: var(--viz-surface); border: 1px solid var(--viz-grid); border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.04); padding: 12px 16px; margin-bottom: 24px; }}
details table {{ margin-top: 10px; }}
summary {{ cursor: pointer; font-size: 13.5px; font-weight: 600; color: var(--viz-text-secondary); }}
.payload-cell summary {{ cursor: pointer; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; color: var(--viz-text-secondary); }}
.payload-cell summary:hover {{ color: var(--viz-text-primary); }}
.payload-cell pre {{ margin: 8px 0 0; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px;
  white-space: pre-wrap; word-break: break-word; max-height: 220px; overflow-y: auto;
  background: var(--viz-page); padding: 8px 10px; border-radius: 6px; }}
td.reason-badge {{ font-weight: 600; font-size: 12.5px; }}
td.reason-prescreen {{ color: var(--viz-series-1); }}
td.reason-bucket {{ color: var(--viz-critical); }}
.tab-bar {{ display: flex; gap: 6px; margin-bottom: 20px; border-bottom: 1px solid var(--viz-grid); }}
.tab-btn {{ background: none; border: none; color: var(--viz-text-secondary); font-weight: 600; font-size: 14px;
  padding: 10px 4px; margin-right: 18px; cursor: pointer; border-bottom: 2px solid transparent; border-radius: 0; }}
.tab-btn:hover {{ color: var(--viz-text-primary); filter: none; }}
.tab-btn.active {{ color: var(--viz-text-primary); border-bottom-color: var(--viz-series-1); }}
.tab-panel[hidden] {{ display: none; }}
.compare-table td:not(:first-child), .compare-table th:not(:first-child) {{ text-align: right; font-variant-numeric: tabular-nums; }}
@media (prefers-color-scheme: dark) {{
  :root {{
    --viz-page: #101112; --viz-surface: #1a1a19; --viz-text-primary: #ffffff; --viz-text-secondary: #c3c2b7;
    --viz-grid: #2c2c2a; --viz-baseline: #383835; --viz-series-1: #3987e5; --viz-series-2: #e5a539;
    --viz-good: #0ca30c; --viz-critical: #d03b3b;
  }}
}}
@media (max-width: 720px) {{
  .charts-grid {{ grid-template-columns: 1fr; }}
  body {{ padding: 20px 16px 48px; }}
}}
</style>
<h1>Trading Dashboard</h1>
<div class="tab-bar">
  <button type="button" class="tab-btn active" data-tab="live" onclick="showDashboardTab('live')">Live / Paper Sessions</button>
  <button type="button" class="tab-btn" data-tab="backtest" onclick="showDashboardTab('backtest')">Portfolio Backtest</button>
</div>
<div id="tab-panel-live" class="tab-panel">
<div class="top-bar">
  <div>
    {f'<select onchange="location.href=&quot;/?session=&quot;+this.value">{session_picker_options}</select>' if recent_sessions else ''}
  </div>
  <div class="right">
    <span id="market-status" class="market-status">checking market hours...</span>
    <label><input type="checkbox" id="auto-refresh-toggle"> Auto-refresh (15s)</label>
  </div>
</div>
{f'<div class="historical-banner">Viewing a past session, not the latest. <a href="/">Back to latest</a></div>' if viewing_historical else ''}
{message_html}
<p class="session-info">Session: <b>{meta.get("session_id", "-")}</b> &middot; Status: <b>{session.get("status")}</b> &middot;
Strategy: {meta.get("strategy_name", "-")} &middot; Broker: {meta.get("broker_provider", "-")}/{meta.get("broker_mode", "-")}</p>

<h2>Limits (this session)</h2>
<div class="hero-card {pnl_card_class}">
  <div>
    <div class="stat-label">Realized P&amp;L</div>
    <div class="hero-value">{pnl_display}</div>
  </div>
  {pnl_spark}
</div>
<div class="stat-grid">
  <div class="stat-card">
    <div class="stat-label">Intraday capital</div>
    <div class="stat-value">{plan.get("intraday_capital", "n/a")}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Trades used / max</div>
    <div class="stat-value">{trades_used}</div>
    <div class="meter-track"><div class="meter-fill" style="width:{trades_meter_pct:.0f}%"></div></div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Max daily loss</div>
    <div class="stat-value">{plan.get("max_daily_loss", "n/a")}</div>
  </div>
</div>

<h2>Mandate settings (next session)</h2>
<div class="card">
  <form class="mandate-form" method="post" action="/update-mandate">
    <div class="field">
      <label>Intraday capital</label>
      <input type="number" step="any" name="intraday_bucket" value="{mandate.buckets.intraday_bucket if mandate else ''}" required>
    </div>
    <div class="field">
      <label>Reserve capital</label>
      <input type="number" step="any" name="reserve_bucket" value="{mandate.buckets.reserve_bucket if mandate else ''}" required>
    </div>
    <div class="field">
      <label>Max daily loss</label>
      <input type="number" step="any" name="max_daily_loss" value="{mandate.risk_limits.max_daily_loss if mandate else ''}" required>
    </div>
    <div class="field">
      <label>Max trades/day</label>
      <input type="number" step="1" min="1" name="max_trades_per_day" value="{mandate.risk_limits.max_trades_per_day if mandate else ''}" required>
    </div>
    <div class="field">
      <label>Stop new entries after</label>
      <input type="time" name="stop_new_entries_after" value="{mandate.risk_limits.stop_new_entries_after if mandate else ''}">
    </div>
    <div class="field">
      <label>Force square-off at</label>
      <input type="time" name="force_square_off_at" value="{mandate.risk_limits.force_square_off_at if mandate else ''}">
    </div>
    <div class="field">
      <label>Risk appetite</label>
      <select name="risk_appetite">
        {"".join(f'<option{" selected" if mandate and mandate.risk_appetite == tier else ""}>{tier}</option>' for tier in sorted(RISK_APPETITES))}
      </select>
    </div>
    <div class="actions">
      <button type="submit">Save mandate</button>
      <span class="mandate-hint">Takes effect on the next session started - won't change what's shown above for this one.</span>
    </div>
  </form>
</div>

<h2>Performance</h2>
<div class="charts-grid">
  <div class="chart-card">
    <h3>Cumulative realized P&amp;L</h3>
    {cumulative_pnl_line_chart(pnl_series)}
  </div>
  <div class="chart-card">
    <h3>Per-trade P&amp;L</h3>
    {trade_pnl_bar_chart(closed_trades)}
  </div>
  <div class="chart-card">
    <h3>Signal funnel</h3>
    {magnitude_bar_chart(funnel_items)}
  </div>
  <div class="chart-card">
    <h3>Rejections by reason</h3>
    {magnitude_bar_chart(rejection_items)}
  </div>
</div>

<h3>Closed trades</h3>
<table><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th></tr>{closed_trade_rows}</table>

<h2>Analysis metrics (all sessions, {portfolio_metrics.trade_count} closed trades)</h2>
<div class="stat-grid">
  <div class="stat-card"><div class="stat-label">Sharpe ratio</div><div class="stat-value">{_fmt_ratio(portfolio_metrics.sharpe_ratio)}</div></div>
  <div class="stat-card"><div class="stat-label">Max drawdown</div><div class="stat-value">-{_fmt_price(portfolio_metrics.max_drawdown)}</div></div>
  <div class="stat-card"><div class="stat-label">Win rate</div><div class="stat-value">{_fmt_pct(portfolio_metrics.win_rate)}</div></div>
  <div class="stat-card"><div class="stat-label">Profit factor</div><div class="stat-value">{_fmt_ratio(portfolio_metrics.profit_factor)}</div></div>
  <div class="stat-card"><div class="stat-label">Avg win</div><div class="stat-value">{_fmt_price(portfolio_metrics.avg_win)}</div></div>
  <div class="stat-card"><div class="stat-label">Avg loss</div><div class="stat-value">{_fmt_price(portfolio_metrics.avg_loss)}</div></div>
  <div class="stat-card"><div class="stat-label">Total trades</div><div class="stat-value">{portfolio_metrics.trade_count}</div></div>
  <div class="stat-card"><div class="stat-label">All-time P&amp;L</div><div class="stat-value">{_fmt_price(portfolio_metrics.total_pnl)}</div></div>
</div>
<p class="mandate-hint">Sharpe is mean/stdev of each trade's return on capital deployed, unannualized (trades don't land on a fixed schedule) - read it as directional, not a textbook figure, and treat every ratio here as noise until trade count is well into the double digits.</p>

<h2>Price forecast (Monte Carlo, {_FORECAST_HORIZON_DAYS}-day)</h2>
{forecast_picker}
{forecast_section_body}

<h2>Candidate selection (why each ticker was picked)</h2>
{candidate_selection_body}

<h2>Universe ({len(all_symbols)} stocks considered, {len(traded)} traded this session)</h2>
<form class="add-stock" method="post" action="/add-symbol">
  <input name="symbol" placeholder="Symbol e.g. WIPRO" required>
  <select name="exchange"><option>NSE</option><option>BSE</option></select>
  <input name="segment" placeholder="Segment" value="CASH">
  <button type="submit">Add to universe</button>
</form>
<details>
<summary>Show all {len(all_symbols)} stocks</summary>
<div class="toolbar"><input type="search" placeholder="Filter universe..." oninput="filterTable(this,'universe-table')"></div>
<table id="universe-table"><tr><th>Symbol</th><th>Status</th></tr>{universe_rows}</table>
</details>

<h2>Final positions</h2>
<table><tr><th>Instrument</th><th>Qty</th><th>Avg price</th></tr>{pos_rows}</table>

<h2>Orders</h2>
<table><tr><th>Instrument</th><th>Side</th><th>Qty</th><th>Avg price</th><th>Status</th></tr>{order_rows}</table>

<h2>Live event log (last 50)</h2>
<div class="toolbar"><input type="search" placeholder="Filter events..." oninput="filterTable(this,'events-table')"></div>
<table id="events-table"><tr><th>Time</th><th>Event</th><th>Symbol</th><th>Payload</th></tr>{event_rows}</table>
</div>

<div id="tab-panel-backtest" class="tab-panel" hidden>
{backtest_tab_html}
</div>

<script>
function showDashboardTab(name) {{
  document.querySelectorAll('.tab-panel').forEach(function (panel) {{
    panel.hidden = panel.id !== 'tab-panel-' + name;
  }});
  document.querySelectorAll('.tab-btn').forEach(function (btn) {{
    btn.classList.toggle('active', btn.dataset.tab === name);
  }});
}}
(function () {{
  const requested = new URLSearchParams(location.search).get('tab');
  if (requested === 'backtest') {{ showDashboardTab('backtest'); }}
}})();
function filterTable(inputEl, tableId) {{
  const q = inputEl.value.toLowerCase();
  document.querySelectorAll('#' + tableId + ' tr:not(:first-child)').forEach(function (row) {{
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}}

document.querySelectorAll('table').forEach(function (table) {{
  const headerRow = table.rows[0];
  Array.from(headerRow.cells).forEach(function (th, colIndex) {{
    th.addEventListener('click', function () {{
      const rows = Array.from(table.rows).slice(1);
      const asc = th.dataset.asc !== 'true';
      rows.sort(function (a, b) {{
        const av = a.cells[colIndex]?.textContent.trim() || '';
        const bv = b.cells[colIndex]?.textContent.trim() || '';
        const an = parseFloat(av), bn = parseFloat(bv);
        const cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : av.localeCompare(bv);
        return asc ? cmp : -cmp;
      }});
      Array.from(headerRow.cells).forEach(function (c) {{ delete c.dataset.asc; }});
      th.dataset.asc = asc;
      rows.forEach(function (row) {{ table.appendChild(row); }});
    }});
  }});
}});

(function () {{
  const tip = document.createElement('div');
  tip.className = 'chart-tooltip';
  tip.hidden = true;
  document.body.appendChild(tip);

  function showTip(el) {{
    tip.textContent = el.getAttribute('data-tip');
    tip.hidden = false;
  }}
  function positionTip(e) {{
    tip.style.left = (e.clientX + 12) + 'px';
    tip.style.top = (e.clientY + 12) + 'px';
  }}
  function hideTip() {{ tip.hidden = true; }}

  document.querySelectorAll('[data-tip]').forEach(function (el) {{
    el.addEventListener('pointerenter', function () {{ showTip(el); }});
    el.addEventListener('pointermove', positionTip);
    el.addEventListener('pointerleave', hideTip);
    el.addEventListener('focus', function () {{ showTip(el); tip.style.left = '12px'; tip.style.top = '12px'; }});
    el.addEventListener('blur', hideTip);
  }});
}})();

function updateMarketStatus() {{
  const statusEl = document.getElementById('market-status');
  if (!statusEl) return;

  // Compute IST regardless of the viewer's own timezone (no DST in IST).
  const now = new Date();
  const istMs = now.getTime() + now.getTimezoneOffset() * 60000 + 5.5 * 3600000;
  const ist = new Date(istMs);
  const day = ist.getDay(); // 0 Sun .. 6 Sat
  const minutesNow = ist.getHours() * 60 + ist.getMinutes();
  const openMin = 9 * 60 + 15;
  const closeMin = 15 * 60 + 30;
  const isWeekday = day >= 1 && day <= 5;

  function fmt(mins) {{
    const h = Math.floor(mins / 60), m = mins % 60;
    return (h > 0 ? h + 'h ' : '') + m + 'm';
  }}

  if (isWeekday && minutesNow >= openMin && minutesNow < closeMin) {{
    statusEl.textContent = 'Market OPEN · closes in ' + fmt(closeMin - minutesNow);
    statusEl.className = 'market-status open';
    return;
  }}

  // NSE holidays aren't known client-side - this assumes every weekday is a
  // trading day, so it can be off by a day around a market holiday.
  let daysAhead = (isWeekday && minutesNow < openMin) ? 0 : 1;
  while ([0, 6].includes((day + daysAhead) % 7)) {{
    daysAhead++;
  }}
  const untilMinutes = daysAhead * 1440 - minutesNow + openMin;
  statusEl.textContent = 'Market CLOSED · opens in ' + fmt(untilMinutes);
  statusEl.className = 'market-status closed';
}}
updateMarketStatus();
setInterval(updateMarketStatus, 1000);

(function () {{
  const toggle = document.getElementById('auto-refresh-toggle');
  if (!toggle) return;
  const enabled = localStorage.getItem('dashboardAutoRefresh') === 'true';
  toggle.checked = enabled;
  if (enabled) {{
    setTimeout(function () {{ location.reload(); }}, 15000);
  }}
  toggle.addEventListener('change', function () {{
    localStorage.setItem('dashboardAutoRefresh', toggle.checked);
    if (toggle.checked) {{
      setTimeout(function () {{ location.reload(); }}, 15000);
    }}
  }});
}})();
</script>
"""


class _DashboardHandler(BaseHTTPRequestHandler):
    def _respond_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _require_auth(self) -> bool:
        if _is_authorized(self):
            return True
        body = b"Authentication required."
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="AQTP Dashboard"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        if not self._require_auth():
            return
        query = parse_qs(urlparse(self.path).query)
        message = query.get("msg", [None])[0]
        severity = query.get("severity", ["info"])[0]

        requested_session_id = query.get("session", [None])[0]
        session_dir = _find_session_dir(requested_session_id) if requested_session_id else None
        if session_dir is None:
            session_dir = _latest_session_dir()
        forecast_symbol = query.get("symbol", [None])[0] or None

        self._respond_html(
            render_html(session_dir, message=message, message_severity=severity, forecast_symbol=forecast_symbol)
        )

    def _redirect_with_message(self, message: str, severity: str = "info") -> None:
        self.send_response(303)
        self.send_header("Location", f"/?msg={quote(message)}&severity={severity}")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        fields = parse_qs(self.rfile.read(length).decode("utf-8"))

        if path == "/add-symbol":
            self._handle_add_symbol(fields)
        elif path == "/update-mandate":
            self._handle_update_mandate(fields)
        else:
            self.send_error(404)

    def _handle_add_symbol(self, fields: dict[str, list[str]]) -> None:
        symbol = fields.get("symbol", [""])[0].strip()
        exchange = fields.get("exchange", ["NSE"])[0].strip()
        segment = fields.get("segment", ["CASH"])[0].strip() or "CASH"
        if symbol and exchange:
            add_instrument_to_universe(UNIVERSE_PATH, symbol, exchange, segment)
        self._redirect_with_message(f"Added {symbol.upper()}:{exchange.upper()} to universe.")

    def _handle_update_mandate(self, fields: dict[str, list[str]]) -> None:
        def get(name: str) -> str:
            return fields.get(name, [""])[0].strip()

        updates: dict[str, object] = {}
        errors: list[str] = []

        for field in ("reserve_bucket", "intraday_bucket", "max_daily_loss"):
            raw = get(field)
            try:
                value = float(raw)
                if value < 0:
                    raise ValueError
                updates[field] = value
            except ValueError:
                errors.append(f"{field} must be a non-negative number")

        raw_trades = get("max_trades_per_day")
        try:
            trades = int(raw_trades)
            if trades <= 0:
                raise ValueError
            updates["max_trades_per_day"] = trades
        except ValueError:
            errors.append("max_trades_per_day must be a positive whole number")

        for field in ("stop_new_entries_after", "force_square_off_at"):
            raw = get(field)
            if raw and not _TIME_PATTERN.match(raw):
                errors.append(f"{field} must be in HH:MM 24-hour format")
            elif raw:
                updates[field] = raw

        risk_appetite = get("risk_appetite")
        if risk_appetite and risk_appetite not in RISK_APPETITES:
            errors.append("risk_appetite must be conservative, moderate, or aggressive")
        elif risk_appetite:
            updates["risk_appetite"] = risk_appetite

        if errors:
            self._redirect_with_message("Mandate NOT updated: " + "; ".join(errors), severity="error")
            return

        try:
            update_daily_mandate_fields(MANDATE_PATH, updates)
        except ValueError as exc:
            self._redirect_with_message(f"Mandate NOT updated: {exc}", severity="error")
            return

        self._redirect_with_message("Mandate updated - takes effect on the next session.")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass


def _resolve_serve_host(host: str | None) -> str:
    """
    Validate and resolve the bind host - pulled out of serve() so the
    guard is testable without ever opening a socket. Raises
    DashboardAuthNotConfiguredError rather than binding somewhere other
    than loopback with no auth configured.
    """
    resolved = host or os.environ.get(HOST_ENV_VAR, LOOPBACK_HOST)
    if resolved != LOOPBACK_HOST and _configured_auth_token() is None:
        raise DashboardAuthNotConfiguredError(
            f"Refusing to bind to {resolved!r} (not {LOOPBACK_HOST}) without "
            f"{AUTH_TOKEN_ENV_VAR} set - this dashboard has POST endpoints that "
            "change live capital/risk settings, and reaching it beyond your own "
            "machine means anyone who finds the address could use them. Set a "
            "random token first, e.g.:\n"
            f'  export {AUTH_TOKEN_ENV_VAR}=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")'
        )
    return resolved


def serve(port: int = 8765, host: str | None = None) -> None:
    host = _resolve_serve_host(host)
    server = ThreadingHTTPServer((host, port), _DashboardHandler)
    print(f"dashboard live at http://{host}:{port} (Ctrl+C to stop)")
    print(f"auth: {'enabled' if _configured_auth_token() else 'disabled (loopback-only)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--serve":
        try:
            serve(int(args[1]) if len(args) > 1 else 8765)
        except DashboardAuthNotConfiguredError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args and args[0] == "--obsidian":
        # imported lazily - apps.reporting.obsidian_export imports from
        # this module, so a top-level import here would be circular.
        from apps.reporting.obsidian_export import sync_loop

        if len(args) < 2:
            print("Usage: python -m apps.reporting.dashboard --obsidian <vault_dir> [interval_seconds]")
            sys.exit(1)
        vault_dir = Path(args[1])
        interval_seconds = float(args[2]) if len(args) > 2 else 15.0
        sync_loop(vault_dir, interval_seconds)
        return

    out = Path(args[0]) if args else Path("data/dashboard.html")
    out.write_text(render_html(_latest_session_dir()))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

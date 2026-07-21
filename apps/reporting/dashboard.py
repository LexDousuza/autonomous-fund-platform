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
"""

from __future__ import annotations

import html
import json
import re
import sys
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from apps.reporting.charts import (
    cumulative_pnl_line_chart,
    magnitude_bar_chart,
    trade_pnl_bar_chart,
)
from core.allocations.daily_mandate import (
    RISK_APPETITES,
    load_daily_mandate,
    update_daily_mandate_fields,
)
from core.analytics.session_performance import compute_closed_trades, compute_cumulative_pnl_series
from core.market_data.universe_loader import add_instrument_to_universe, load_instrument_universe

SESSIONS_DIR = Path("data/live_sessions")
UNIVERSE_PATH = "config/instruments/india_cash_universe.yaml"
MANDATE_PATH = "config/mandates/sample_mandate.yaml"
_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _all_session_dirs() -> list[Path]:
    """All session directories, oldest first (matches the sortable session-id naming)."""
    if not SESSIONS_DIR.exists():
        return []
    return sorted((d for d in SESSIONS_DIR.iterdir() if d.is_dir()), key=lambda p: p.name)


def _latest_session_dir() -> Path | None:
    dirs = _all_session_dirs()
    return dirs[-1] if dirs else None


def _find_session_dir(session_id: str) -> Path | None:
    candidate = SESSIONS_DIR / session_id
    return candidate if candidate.is_dir() else None


def _load_events(session_dir: Path) -> list[dict]:
    events_path = session_dir / "events.jsonl"
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]


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


def _order_row(o: dict) -> str:
    return (
        f'<tr><td>{o.get("exchange")}:{o.get("symbol")}</td><td>{o.get("side")}</td>'
        f'<td>{o.get("quantity")}</td><td>{o.get("average_price")}</td><td>{o.get("status")}</td></tr>'
    )


def render_html(
    session_dir: Path | None, message: str | None = None, message_severity: str = "info"
) -> str:
    universe = load_instrument_universe(UNIVERSE_PATH)
    all_symbols = sorted({i.symbol for i in universe.enabled_instruments()})

    try:
        mandate = load_daily_mandate(MANDATE_PATH)
    except (FileNotFoundError, ValueError):
        mandate = None

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

    universe_rows = "".join(
        f'<tr><td>{html.escape(s)}</td>'
        f'<td>{"traded this session" if s in traded else "not selected"}</td></tr>'
        for s in all_symbols
    )

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
    closed_trade_rows = "".join(
        f'<tr><td>{html.escape(t.symbol)}</td><td>{t.entry_price:.2f}</td><td>{t.exit_price:.2f}</td>'
        f'<td>{t.quantity}</td><td>{t.pnl:+,.2f}</td></tr>'
        for t in closed_trades
    ) or "<tr><td colspan=5>no closed trades yet</td></tr>"

    rejection_counts = Counter(
        (e.get("payload") or {}).get("reason") or (e.get("payload") or {}).get("rejection_stage") or "unknown"
        for e in events
        if e.get("event_type") == "signal_rejected"
    )
    rejection_items = sorted(rejection_counts.items(), key=lambda kv: -kv[1])

    event_rows = "".join(
        f'<tr><td>{e.get("timestamp", "")}</td><td>{e.get("event_type")}</td>'
        f'<td>{e.get("symbol") or ""}</td><td>{json.dumps(e.get("payload", {}))}</td></tr>'
        for e in events[-50:]
    ) or "<tr><td colspan=4>no events</td></tr>"

    message_html = (
        f'<p class="msg msg-{html.escape(message_severity)}">{html.escape(message)}</p>' if message else ""
    )

    return f"""<title>Trading Dashboard</title>
<meta name="color-scheme" content="light dark">
<style>
:root {{
  --viz-page: #f4f3f0; --viz-surface: #fcfcfb; --viz-text-primary: #0b0b0b; --viz-text-secondary: #52514e;
  --viz-grid: #e1e0d9; --viz-baseline: #c3c2b7; --viz-series-1: #2a78d6;
  --viz-good: #0ca30c; --viz-critical: #d03b3b;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  padding: 32px 40px 64px; max-width: 1100px; margin: auto;
  background: var(--viz-page); color: var(--viz-text-primary); line-height: 1.5;
}}
h1 {{ font-size: 26px; font-weight: 700; margin: 0 0 20px; letter-spacing: -0.01em; }}
h2 {{ font-size: 18px; font-weight: 600; margin: 40px 0 14px; letter-spacing: -0.01em; }}
h2:first-of-type {{ margin-top: 28px; }}
h3 {{ font-size: 13px; font-weight: 600; color: var(--viz-text-secondary); margin: 0 0 12px; text-transform: uppercase; letter-spacing: 0.04em; }}
p {{ color: var(--viz-text-secondary); font-size: 14px; }}
.card {{ background: var(--viz-surface); border: 1px solid var(--viz-grid); border-radius: 10px; padding: 18px 20px; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; background: var(--viz-surface);
  border: 1px solid var(--viz-grid); border-radius: 10px; overflow: hidden; }}
th, td {{ border-bottom: 1px solid var(--viz-grid); padding: 9px 14px; text-align: left; font-size: 13.5px; }}
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
  margin-bottom: 20px; padding: 10px 16px; background: var(--viz-surface); border: 1px solid var(--viz-grid); border-radius: 10px; }}
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
.stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 24px; }}
.stat-card {{ background: var(--viz-surface); border: 1px solid var(--viz-grid); border-radius: 10px; padding: 14px 16px; }}
.stat-card .stat-label {{ font-size: 12px; color: var(--viz-text-secondary); text-transform: uppercase; letter-spacing: 0.03em; }}
.stat-card .stat-value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
.charts-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }}
.chart-card {{ background: var(--viz-surface); border: 1px solid var(--viz-grid); border-radius: 10px; padding: 18px 20px; }}
.chart-card h3 {{ margin: 0 0 12px; }}
.chart-empty {{ color: var(--viz-text-secondary); font-size: 13px; }}
.viz-chart {{ width: 100%; height: auto; display: block; overflow: visible; }}
.viz-baseline {{ stroke: var(--viz-baseline); stroke-width: 1; }}
.viz-grid {{ stroke: var(--viz-grid); stroke-width: 1; }}
.viz-axis {{ fill: var(--viz-text-secondary); font-size: 12px; }}
.viz-label {{ fill: var(--viz-text-primary); font-size: 13px; font-weight: 600; }}
.viz-label-sm {{ fill: var(--viz-text-primary); font-size: 11px; font-weight: 600; }}
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
@media (prefers-color-scheme: dark) {{
  :root {{
    --viz-page: #101112; --viz-surface: #1a1a19; --viz-text-primary: #ffffff; --viz-text-secondary: #c3c2b7;
    --viz-grid: #2c2c2a; --viz-baseline: #383835; --viz-series-1: #3987e5;
    --viz-good: #0ca30c; --viz-critical: #e66767;
  }}
}}
@media (max-width: 720px) {{
  .charts-grid {{ grid-template-columns: 1fr; }}
  body {{ padding: 20px 16px 48px; }}
}}
</style>
<h1>Trading Dashboard</h1>
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
  <div class="stat-card">
    <div class="stat-label">Realized P&amp;L</div>
    <div class="stat-value">{pnl}</div>
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

<h2>Universe ({len(all_symbols)} stocks considered, {len(traded)} traded this session)</h2>
<form class="add-stock" method="post" action="/add-symbol">
  <input name="symbol" placeholder="Symbol e.g. WIPRO" required>
  <select name="exchange"><option>NSE</option><option>BSE</option></select>
  <input name="segment" placeholder="Segment" value="CASH">
  <button type="submit">Add to universe</button>
</form>
<div class="toolbar"><input type="search" placeholder="Filter universe..." oninput="filterTable(this,'universe-table')"></div>
<table id="universe-table"><tr><th>Symbol</th><th>Status</th></tr>{universe_rows}</table>

<h2>Final positions</h2>
<table><tr><th>Instrument</th><th>Qty</th><th>Avg price</th></tr>{pos_rows}</table>

<h2>Orders</h2>
<table><tr><th>Instrument</th><th>Side</th><th>Qty</th><th>Avg price</th><th>Status</th></tr>{order_rows}</table>

<h2>Live event log (last 50)</h2>
<div class="toolbar"><input type="search" placeholder="Filter events..." oninput="filterTable(this,'events-table')"></div>
<table id="events-table"><tr><th>Time</th><th>Event</th><th>Symbol</th><th>Payload</th></tr>{event_rows}</table>

<script>
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

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        query = parse_qs(urlparse(self.path).query)
        message = query.get("msg", [None])[0]
        severity = query.get("severity", ["info"])[0]

        requested_session_id = query.get("session", [None])[0]
        session_dir = _find_session_dir(requested_session_id) if requested_session_id else None
        if session_dir is None:
            session_dir = _latest_session_dir()

        self._respond_html(render_html(session_dir, message=message, message_severity=severity))

    def _redirect_with_message(self, message: str, severity: str = "info") -> None:
        self.send_response(303)
        self.send_header("Location", f"/?msg={quote(message)}&severity={severity}")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
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


def serve(port: int = 8765) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), _DashboardHandler)
    print(f"dashboard live at http://127.0.0.1:{port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--serve":
        serve(int(args[1]) if len(args) > 1 else 8765)
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

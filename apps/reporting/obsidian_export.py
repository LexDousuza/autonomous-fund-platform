"""
Obsidian-vault Markdown export of the trading dashboard.

Same underlying session data as apps/reporting/dashboard.py's HTML view,
but as plain Markdown tables Obsidian renders natively - no JS, so no
sortable columns, hover tooltips, or add-stock form (see dashboard.py
for those; edit config/instruments/india_cash_universe.yaml directly, or
use the HTML dashboard, to add a stock instead).

"Live" here means something different than the HTML version's
JS-driven auto-refresh: this writes a Markdown file into the vault on a
loop, and Obsidian auto-reloads a note when its underlying file changes
on disk - so an open note updates on its own with no plugin required.

Two note types, matching how Obsidian is actually used (linked files,
not a single dropdown-driven page):

- ``Trading Dashboard.md``  always overwritten with the *current*
  session's live state - this is "the dashboard."
- ``Sessions/<session_id>.md``  a permanent snapshot, written once a
  session reaches a terminal status (completed/failed) and never
  touched again - this is how you browse history, via Obsidian's normal
  file browser/search, not a fake in-page picker.

    python -m apps.reporting.dashboard --obsidian /path/to/vault [interval_seconds]
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apps.reporting.dashboard import (
    SESSIONS_DIR,
    UNIVERSE_PATH,
    _all_session_dirs,
    _latest_candidate_selection,
    _latest_session_dir,
    _load_events,
)
from core.analytics.session_performance import compute_closed_trades, compute_cumulative_pnl_series
from core.market_data.universe_loader import load_instrument_universe

VAULT_SUBFOLDER = "Trading"
_IST = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN = (9, 15)
_MARKET_CLOSE = (15, 30)


def _format_timedelta(td: timedelta) -> str:
    total_minutes = max(0, int(td.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _market_status_line() -> str:
    """
    Static (render-time) IST market-open/closed status - the Markdown
    equivalent of dashboard.py's live JS countdown, since Obsidian can't
    run JS. Freshness depends on how often the file gets regenerated
    (see sync_loop).
    """
    now = datetime.now(_IST)
    is_weekday = now.weekday() < 5  # Mon=0 .. Sun=6
    open_today = now.replace(hour=_MARKET_OPEN[0], minute=_MARKET_OPEN[1], second=0, microsecond=0)
    close_today = now.replace(hour=_MARKET_CLOSE[0], minute=_MARKET_CLOSE[1], second=0, microsecond=0)

    if is_weekday and open_today <= now < close_today:
        return f"Market OPEN · closes in {_format_timedelta(close_today - now)}"

    days_ahead = 0 if (is_weekday and now < open_today) else 1
    candidate = now + timedelta(days=days_ahead)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    next_open = candidate.replace(hour=_MARKET_OPEN[0], minute=_MARKET_OPEN[1], second=0, microsecond=0)
    return f"Market CLOSED · opens in {_format_timedelta(next_open - now)}"


def _text_meter(pct: float, width: int = 20) -> str:
    filled = round(width * min(100.0, max(0.0, pct)) / 100)
    return "`[" + "█" * filled + "░" * (width - filled) + "]`"


def _escape_md_cell(value: object) -> str:
    """
    A literal ``|`` in cell content (common in real news headlines, e.g.
    "X | Y" title formats) would otherwise be read as a column
    separator and corrupt the table - escape it, and collapse any
    embedded newline, since a Markdown table row must stay on one line.
    """
    return str(value).replace("|", "\\|").replace("\n", " ")


def _md_table(headers: list[str], rows: list[list[str]], empty_message: str) -> str:
    if not rows:
        return f"*{empty_message}*\n"
    header_line = "| " + " | ".join(headers) + " |"
    sep_line = "| " + " | ".join("---" for _ in headers) + " |"
    row_lines = "\n".join("| " + " | ".join(_escape_md_cell(c) for c in row) + " |" for row in rows)
    return f"{header_line}\n{sep_line}\n{row_lines}\n"


def _candidate_selection_section() -> list[str]:
    """
    The Markdown equivalent of dashboard.py's "Candidate selection" HTML
    section - same source (core/market_data/candidate_selection_store.py),
    same "why" fields, just table cells instead of <details>/<summary>
    (Obsidian's renderer supports inline HTML like <br> inside a cell,
    which is what makes a multi-headline list fit in one row).
    """
    selection = _latest_candidate_selection()
    if selection is None:
        return [
            "## Candidate selection",
            "",
            "*No overnight candidate selection saved yet - see "
            "`scripts/run_overnight_selection.sh`. Sessions fall back to selecting live in the meantime.*",
            "",
        ]

    if selection.selected_detail:
        selected_rows = [
            [
                f'{row["exchange"]}:{row["symbol"]}',
                f'₹{row["last_close"]:,.2f}' if row.get("last_close") is not None else "-",
                (
                    f'{row["volatility_pct"] * 100:.2f}% vs {row.get("volatility_ceiling", 0) * 100:.2f}% '
                    f'ceiling ({row.get("volatility_source", "")})'
                    if row.get("volatility_pct") is not None
                    else "-"
                ),
                (
                    f'score {row["news_score"]}<br>' + "<br>".join(f"- {h}" for h in (row.get("headlines") or []))
                    if row.get("news_score") is not None
                    else "not news-checked"
                ),
            ]
            for row in selection.selected_detail
        ]
    else:
        # A file saved before this reasoning existed - fall back to the
        # bare symbol list rather than an empty table (see dashboard.py's
        # identical fallback for the same reason).
        selected_rows = [[f"{i.exchange}:{i.symbol}", "-", "-", "no detail saved for this selection"] for i in selection.instruments]

    excluded_reason_counts = Counter(row.get("reason") for row in selection.excluded_detail)
    excluded_summary = (
        ", ".join(f"{reason}: {count}" for reason, count in sorted(excluded_reason_counts.items(), key=lambda kv: -kv[1]))
        or "none"
    )

    return [
        "## Candidate selection",
        "",
        f"**Trading date:** {selection.trading_date} · **Computed:** {selection.computed_at} · "
        f"**{len(selection.instruments)} selected, {len(selection.excluded_detail)} excluded**",
        "",
        _md_table(["Instrument", "Last close", "Volatility", "News"], selected_rows, "none selected"),
        f"**Excluded ({len(selection.excluded_detail)})** - by reason: {excluded_summary}",
        "",
        "> [!note]- Full excluded list",
        ">",
        "> "
        + _md_table(
            ["Instrument", "Reason", "Detail"],
            [
                [
                    f'{row["exchange"]}:{row["symbol"]}',
                    f'{row.get("stage", "")}: {row.get("reason", "")}',
                    ", ".join(
                        bit
                        for bit in [
                            f'LTP ₹{row["ltp"]:,.2f}' if row.get("ltp") is not None else None,
                            f'close ₹{row["last_close"]:,.2f}' if row.get("last_close") is not None else None,
                            (
                                f'vol {row["volatility_pct"] * 100:.2f}% vs {row.get("volatility_ceiling", 0) * 100:.2f}%'
                                if row.get("volatility_pct") is not None
                                else None
                            ),
                            f'news score {row["news_score"]}' if row.get("news_score") is not None else None,
                        ]
                        if bit is not None
                    )
                    or "-",
                ]
                for row in selection.excluded_detail
            ],
            "none excluded",
        ).replace("\n", "\n> "),
        "",
    ]


def render_markdown(session_dir: Path | None) -> str:
    """Build the Markdown content for the always-latest 'Trading Dashboard' note."""
    universe = load_instrument_universe(UNIVERSE_PATH)
    all_symbols = sorted({i.symbol for i in universe.enabled_instruments()})

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

    if summary:
        risk = summary.get("final_session_risk_state") or {}
        pnl = risk.get("realized_pnl", 0)
        entries_taken = risk.get("entries_taken", 0)
        positions = summary.get("final_positions", [])
        orders = summary.get("placed_orders", []) + summary.get("exit_orders", [])
        funnel_items = [
            ("Generated", summary.get("generated_signals", 0)),
            ("Approved", summary.get("approved_signals", 0)),
            ("Rejected", summary.get("rejected_signals", 0)),
        ]
    else:
        pnl = "session in progress"
        entries_taken = sum(1 for e in events if e.get("event_type") == "order_placed")
        positions = []
        orders = []
        funnel_items = [
            ("Generated", sum(1 for e in events if e.get("event_type") == "signal_generated")),
            ("Approved", sum(1 for e in events if e.get("event_type") == "signal_approved")),
            ("Rejected", sum(1 for e in events if e.get("event_type") == "signal_rejected")),
        ]

    max_trades = plan.get("max_trades_per_day")
    trades_meter_pct = min(100.0, entries_taken / max_trades * 100) if max_trades else 0.0
    trades_used_line = f'{entries_taken} / {max_trades if max_trades is not None else "n/a"} {_text_meter(trades_meter_pct)}'

    closed_trades = compute_closed_trades(events)
    pnl_series = compute_cumulative_pnl_series(closed_trades)
    cumulative_pnl = pnl_series[-1][1] if pnl_series else 0.0

    rejection_counts = Counter(
        (e.get("payload") or {}).get("reason") or (e.get("payload") or {}).get("rejection_stage") or "unknown"
        for e in events
        if e.get("event_type") == "signal_rejected"
    )
    rejection_items = sorted(rejection_counts.items(), key=lambda kv: -kv[1])

    updated_at = datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")

    lines = [
        "---",
        f'session_id: "{meta.get("session_id", "-")}"',
        f'status: "{session.get("status")}"',
        f'strategy: "{meta.get("strategy_name", "-")}"',
        f'updated: "{updated_at}"',
        "---",
        "",
        "# Trading Dashboard",
        "",
        f"> {_market_status_line()} (as of {updated_at})",
        "",
        f'**Session:** `{meta.get("session_id", "-")}` · **Status:** {session.get("status")} · '
        f'**Strategy:** {meta.get("strategy_name", "-")} · '
        f'**Broker:** {meta.get("broker_provider", "-")}/{meta.get("broker_mode", "-")}',
        "",
        "## Limits",
        "",
        _md_table(
            ["Metric", "Value"],
            [
                ["Intraday capital", plan.get("intraday_capital", "n/a")],
                ["Trades used / max", trades_used_line],
                ["Max daily loss", plan.get("max_daily_loss", "n/a")],
                ["Realized P&L", f"{pnl:+,.2f}" if isinstance(pnl, (int, float)) else pnl],
                ["Cumulative closed-trade P&L", f"{cumulative_pnl:+,.2f}"],
            ],
            "no data yet",
        ),
        "## Closed trades",
        "",
        _md_table(
            ["Symbol", "Entry", "Exit", "Qty", "P&L"],
            [
                [t.symbol, f"{t.entry_price:.2f}", f"{t.exit_price:.2f}", t.quantity, f"{t.pnl:+,.2f}"]
                for t in closed_trades
            ],
            "no closed trades yet",
        ),
        "## Signal funnel",
        "",
        _md_table(["Stage", "Count"], [[label, count] for label, count in funnel_items], "no data yet"),
        "## Rejections by reason",
        "",
        _md_table(["Reason", "Count"], [[label, count] for label, count in rejection_items], "no rejections"),
        *_candidate_selection_section(),
        f"## Universe ({len(all_symbols)} stocks, {len(traded)} traded this session)",
        "",
        f"> [!note]- Show all {len(all_symbols)} stocks",
        ">",
        "> "
        + _md_table(
            ["Symbol", "Status"],
            [[s, "traded this session" if s in traded else "not selected"] for s in all_symbols],
            "no universe loaded",
        ).replace("\n", "\n> "),
        "## Final positions",
        "",
        _md_table(
            ["Instrument", "Qty", "Avg price"],
            [[f'{p["exchange"]}:{p["symbol"]}', p["quantity"], p["average_price"]] for p in positions],
            "no positions",
        ),
        "## Orders",
        "",
        _md_table(
            ["Instrument", "Side", "Qty", "Avg price", "Status"],
            [
                [f'{o.get("exchange")}:{o.get("symbol")}', o.get("side"), o.get("quantity"), o.get("average_price"), o.get("status")]
                for o in orders
            ],
            "no orders yet",
        ),
        "---",
        "",
        f"See `{VAULT_SUBFOLDER}/Sessions/` for past sessions. "
        f"*Auto-generated - edits here are overwritten on the next sync.*",
        "",
    ]
    return "\n".join(lines)


def sync_once(vault_dir: Path) -> Path | None:
    """
    Write the live 'Trading Dashboard.md' note, and - only the first
    time a session is seen in a terminal status - a permanent snapshot
    under 'Sessions/'. Returns the session dir just synced, if any.
    """
    trading_dir = vault_dir / VAULT_SUBFOLDER
    sessions_dir = trading_dir / "Sessions"
    trading_dir.mkdir(parents=True, exist_ok=True)

    session_dir = _latest_session_dir()
    (trading_dir / "Trading Dashboard.md").write_text(render_markdown(session_dir), encoding="utf-8")

    if session_dir is not None:
        status = json.loads((session_dir / "session.json").read_text()).get("status")
        snapshot_path = sessions_dir / f"{session_dir.name}.md"
        if status in ("completed", "failed") and not snapshot_path.exists():
            sessions_dir.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_text(render_markdown(session_dir), encoding="utf-8")

    return session_dir


def sync_loop(vault_dir: Path, interval_seconds: float = 15.0) -> None:
    """Regenerate the vault notes every ``interval_seconds`` until interrupted (Ctrl-C)."""
    print(f"syncing to Obsidian vault at {vault_dir / VAULT_SUBFOLDER} every {interval_seconds}s (Ctrl-C to stop)")
    try:
        while True:
            sync_once(vault_dir)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        pass

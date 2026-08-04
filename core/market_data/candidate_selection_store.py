"""
Persists/loads a day's already-decided tradable symbol list, so an
expensive candidate-selection run (prescreen + historical fetch + bucket
selection - see core/market_data/candidate_prescreen.py's module
docstring on why that can take an hour+ against Angel One's full NSE
universe) can happen once, overnight, and the market-open trading session
just reads the result instead of repeating all of it starting at 9 AM.

One JSON file per trading date (``data/candidate_selection/<date>.json``)
- deliberately not a database: this platform has no other persistent
store beyond flat files (``data/live_sessions/``, ``data/audit/``), and a
single file per date is trivially inspectable and safe to delete if
something looks wrong the next morning.

``instruments`` (plain ``symbol``/``exchange``) is what
``apps/live_trader/main.py``'s fast path actually trades on - kept typed
since trading logic depends on it. ``selected_detail``/``excluded_detail``
(the *why* - LTP, news score/headlines, volatility vs. ceiling) are
reporting-only, so they're passed through as plain dicts rather than
importing ``core/market_data/bucket_selector.py``'s and
``candidate_prescreen.py``'s dataclasses here - this store's job is
serializing whatever detail its caller already computed, not owning that
shape. The dashboard (``apps/reporting/dashboard.py``) reads them to
answer "why was this ticker picked."
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from core.market_data.universe_models import Instrument

_DEFAULT_DIR = Path("data/candidate_selection")


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    trading_date: str
    computed_at: str
    instruments: list[Instrument]
    selected_detail: list[dict] = field(default_factory=list)
    excluded_detail: list[dict] = field(default_factory=list)


def _path_for(trading_date: str, directory: Path, strategy_name: str | None = None) -> Path:
    if strategy_name is None:
        return directory / f"{trading_date}.json"
    return directory / f"{trading_date}_{strategy_name}.json"


def save_candidate_selection(
    trading_date: date | str,
    instruments: list[Instrument],
    directory: str | Path = _DEFAULT_DIR,
    selected_detail: list[dict] | None = None,
    excluded_detail: list[dict] | None = None,
    strategy_name: str | None = None,
) -> Path:
    """
    Write ``instruments`` (just ``symbol``/``exchange`` each) as today's
    selection, overwriting any existing file for the same
    ``trading_date``/``strategy_name`` - re-running the overnight job for
    the same date is meant to replace, not append to, its own prior
    result.

    ``trading_date`` accepts a real ``date`` (what
    ``PortfolioAllocationPlan.trading_date`` actually is) or a
    ``"YYYY-MM-DD"`` string - always normalized to the string form for
    both the filename and the stored payload, so ``load_candidate_selection``
    always gets a plain string back regardless of which was passed in.

    ``strategy_name`` is optional: when given, the selection is written
    to a strategy-suffixed file (``<date>_<strategy_name>.json``) instead
    of the legacy shared one, since selection is now strategy-aware (see
    ``core.market_data.candidate_profile.CandidateProfile``) - different
    strategies can legitimately want different daily buckets.  Omitting
    it reproduces the exact pre-existing filename/behavior.

    ``selected_detail``/``excluded_detail`` are opaque JSON-serializable
    dicts (see module docstring) - written through as-is, no schema
    enforced here.
    """
    trading_date = str(trading_date)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    payload = {
        "trading_date": trading_date,
        "computed_at": datetime.now(UTC).isoformat(),
        "symbols": [{"symbol": i.symbol, "exchange": i.exchange} for i in instruments],
        "selected_detail": selected_detail or [],
        "excluded_detail": excluded_detail or [],
    }
    path = _path_for(trading_date, directory, strategy_name)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_candidate_selection(
    trading_date: date | str,
    directory: str | Path = _DEFAULT_DIR,
    strategy_name: str | None = None,
) -> CandidateSelection | None:
    """
    Return the persisted selection for ``trading_date``, or ``None`` if
    no file exists for that date - the normal, expected case whenever an
    overnight selection run hasn't happened (or hasn't happened *yet*
    for today), which callers treat as "fall back to selecting live."

    ``strategy_name`` looks up the strategy-suffixed file (see
    ``save_candidate_selection``) - still ``None`` (not a fallback to
    the legacy shared file) when a strategy has its own filename slot
    but nothing was saved there yet, so a caller can deliberately try
    the strategy-specific file first and the legacy shared one second.

    A file that exists but is malformed raises instead of silently
    falling back - that would hide a real problem (a half-written file,
    a schema change) behind what looks like "no selection was saved
    yet," which working code should never do (see this repo's
    fail-loudly-during-development stance elsewhere, e.g.
    core/market_data/angelone_historical_feed.py's window-ceiling check).
    """
    path = _path_for(str(trading_date), Path(directory), strategy_name)
    if not path.exists():
        return None

    payload = json.loads(path.read_text(encoding="utf-8"))
    instruments = [
        Instrument(symbol=item["symbol"], exchange=item["exchange"]) for item in payload["symbols"]
    ]
    return CandidateSelection(
        trading_date=payload["trading_date"],
        computed_at=payload["computed_at"],
        instruments=instruments,
        selected_detail=payload.get("selected_detail", []),
        excluded_detail=payload.get("excluded_detail", []),
    )

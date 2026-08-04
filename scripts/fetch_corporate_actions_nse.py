#!/usr/bin/env python3
"""
Offline fetch of real NSE corporate-actions history (2016-present, free
text: dividends, bonuses, splits, rights, demergers, mergers, buybacks)
for scripts.liquid_universe.LIQUID_UNIVERSE_SYMBOLS, from NSE's own
public corporates-corporateActions endpoint - the same free-but-
bot-filtered public archive scripts/fetch_historical_options_nse.py
already uses for bhavcopy, verified working the same way (a session-
cookie handshake against the human-facing page first, then the API with
a browser-like User-Agent/Referer - see this module's _fetch_symbol).

Written to closes the gap docs/research/data_quality_audit_phase1.md
flagged and docs/research/data/universe_expansion_2026-07-30.md then hit
for real (SKFINDIA's unadjusted demerger). See
core/market_data/corporate_actions.py for the classifier/adjuster this
table feeds.

    set -a; source .env.live_trader; set +a   # not actually needed - no auth required, kept for consistency
    PYTHONPATH=. python scripts/fetch_corporate_actions_nse.py

No credentials required - nseindia.com publishes this for free, gated
only by basic bot filtering (a session cookie + browser-like headers),
not a paywall.
"""

from __future__ import annotations

import csv
import http.cookiejar
import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime

from scripts.liquid_universe import LIQUID_UNIVERSE_SYMBOLS

OUTPUT_PATH = "data/processed/nse_corporate_actions.csv"
FROM_DATE = date(2016, 1, 1)
_REQUEST_PACING_SECONDS = 1.0  # polite pacing against a shared public exchange archive

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_LANDING_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
_API_URL = "https://www.nseindia.com/api/corporates-corporateActions"
_HEADERS = {"User-Agent": _UA, "Referer": _LANDING_PAGE, "Accept": "application/json"}


def _build_opener() -> urllib.request.OpenerDirector:
    """One cookie jar/opener reused across every symbol - NSE's session cookie is what the API gate actually checks."""
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    opener.open(urllib.request.Request(_LANDING_PAGE, headers=_HEADERS), timeout=15)
    return opener


def _fetch_symbol(opener: urllib.request.OpenerDirector, symbol: str, from_date: date, to_date: date) -> list[dict]:
    url = (
        f"{_API_URL}?index=equities&symbol={symbol}"
        f"&from_date={from_date.strftime('%d-%m-%Y')}&to_date={to_date.strftime('%d-%m-%Y')}"
    )
    response = opener.open(urllib.request.Request(url, headers=_HEADERS), timeout=20)
    return json.loads(response.read().decode("utf-8"))


def _parse_ex_date(raw: str) -> date | None:
    """NSE uses '-' for an unset date field, and 'DD-Mon-YYYY' (e.g. '07-Sep-2017') otherwise."""
    if not raw or raw == "-":
        return None
    return datetime.strptime(raw, "%d-%b-%Y").date()


def main() -> None:
    opener = _build_opener()
    today = date.today()
    print(f"Fetching corporate actions {FROM_DATE} to {today} for {len(LIQUID_UNIVERSE_SYMBOLS)} symbols...")

    rows: list[list[object]] = []
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []

    for i, symbol in enumerate(LIQUID_UNIVERSE_SYMBOLS):
        if i > 0:
            time.sleep(_REQUEST_PACING_SECONDS)
        try:
            records = _fetch_symbol(opener, symbol, FROM_DATE, today)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            print(f"  {symbol}: FAILED ({exc})")
            failed.append((symbol, str(exc)))
            continue

        symbol_rows = 0
        for record in records:
            ex_date = _parse_ex_date(record.get("exDate", ""))
            subject = (record.get("subject") or "").strip()
            if ex_date is None or not subject:
                continue
            rows.append([symbol, ex_date.isoformat(), subject])
            symbol_rows += 1
        succeeded.append(symbol)
        print(f"  {symbol}: {symbol_rows} actions")

    print()
    print(f"Succeeded ({len(succeeded)}/{len(LIQUID_UNIVERSE_SYMBOLS)})")
    print(f"Failed ({len(failed)}/{len(LIQUID_UNIVERSE_SYMBOLS)}): {', '.join(s for s, _ in failed) or 'none'}")
    for symbol, reason in failed:
        print(f"  {symbol}: {reason}")

    # Same refuse-to-overwrite-on-total-failure lesson as
    # fetch_historical_bars.py/fetch_historical_bars_angelone.py - see
    # docs/research/data/universe_expansion_2026-07-30.md for why.
    if not succeeded:
        print(f"\nREFUSING to write {OUTPUT_PATH}: 0/{len(LIQUID_UNIVERSE_SYMBOLS)} symbols succeeded - "
              "leaving any existing file untouched.")
        return

    rows.sort(key=lambda r: (r[0], r[1]))
    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "ex_date", "subject"])
        writer.writerows(rows)

    print(f"\nTotal actions written: {len(rows)}")
    print(f"Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

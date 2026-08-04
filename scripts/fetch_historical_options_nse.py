#!/usr/bin/env python3
"""
Offline backfill of historical NSE index-options implied volatility
(NIFTY/BANKNIFTY) from NSE's own public daily bhavcopy archive, run
through core/pricing/black_scholes.py's stdlib IV solver - the free
alternative to a paid options-history vendor. There's no historical
option-chain/IV data source wired into the platform otherwise (see
core/strategy/credit_spread.py's strategy - it can currently only be
forward/paper-tested, never backtested).

NSE has published bhavcopy in two formats over the backfill window:
- UDiFF (2024-01-02 onward): one row per contract, already carries the
  underlying's settlement price (``UndrlygPric``) - no extra join needed.
- Pre-UDiFF (back to at least 2016): no underlying-price column - this
  script derives spot from the same day's nearest-expiry index future
  instead (``FUTIDX`` rows, already in the same file, not a second
  fetch) - a small basis approximation, see ``_parse_old``'s comment.

Both are tried per date (whichever the date's year suggests first, the
other as fallback on a 404) since the exact cutover day isn't worth
hardcoding.

Requires no broker account and no API key - nseindia.com publishes this
for free, but blocks requests without a browser-like User-Agent/Referer
(basic bot filtering on an otherwise public archive, not a paywall).

    PYTHONPATH=. python scripts/fetch_historical_options_nse.py [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--out PATH]

Resumable: appends to --out and skips writing a new header if it
already exists, so a killed/interrupted run can restart with --start
set to wherever it left off.
"""

from __future__ import annotations

import argparse
import csv
import io
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

from core.broker.base import OptionType
from core.market_data.fo_iv_screen import DEFAULT_RISK_FREE_RATE
from core.pricing.black_scholes import implied_volatility

DEFAULT_OUT_PATH = "data/processed/nse_index_options_iv_history.csv"
DEFAULT_START = date(2016, 1, 1)
SYMBOLS = {"NIFTY", "BANKNIFTY"}
_UDIFF_CUTOVER = date(2024, 1, 1)  # heuristic only - the other format is always tried as fallback
_REQUEST_PACING_SECONDS = 0.5  # polite pacing against a shared public exchange archive, not a rate-limited paid API
_DAYS_PER_YEAR = 365.0

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_HEADERS = {"User-Agent": _UA, "Referer": "https://www.nseindia.com/all-reports-derivatives"}

_UDIFF_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
_OLD_URL = "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{yyyy}/{mon}/fo{ddmonyyyy}bhav.csv.zip"


def _old_url(d: date) -> str:
    return _OLD_URL.format(yyyy=d.year, mon=d.strftime("%b").upper(), ddmonyyyy=d.strftime("%d%b%Y").upper())


def _udiff_url(d: date) -> str:
    return _UDIFF_URL.format(ymd=d.strftime("%Y%m%d"))


def _fetch_zip_csv(url: str) -> str | None:
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            return zf.read(name).decode("utf-8", errors="replace")
    except (
        urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
        ConnectionError, zipfile.BadZipFile, StopIteration,
    ):
        return None


def _fetch_day_csv(d: date) -> tuple[str, str] | None:
    """Returns (csv_text, format_name), trying the date-appropriate format first, the other as fallback."""
    formats = [("udiff", _udiff_url), ("old", _old_url)]
    if d < _UDIFF_CUTOVER:
        formats.reverse()
    for fmt_name, url_fn in formats:
        text = _fetch_zip_csv(url_fn(d))
        if text is not None:
            return text, fmt_name
        time.sleep(_REQUEST_PACING_SECONDS)
    return None


def _time_to_expiry_years(trade_date: date, expiry: date) -> float:
    return max((expiry - trade_date).days, 0) / _DAYS_PER_YEAR


def _parse_udiff(csv_text: str) -> list[dict]:
    rows = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        symbol = row.get("TckrSymb")
        if symbol not in SYMBOLS or row.get("OptnTp") not in ("CE", "PE"):
            continue
        try:
            rows.append(dict(
                symbol=symbol,
                expiry=datetime.strptime(row["XpryDt"], "%Y-%m-%d").date(),
                strike=float(row["StrkPric"]),
                option_type=row["OptnTp"],
                settle=float(row["SttlmPric"]),
                spot=float(row["UndrlygPric"]),
                oi=int(float(row["OpnIntrst"])),
                volume=int(float(row["TtlTradgVol"])),
            ))
        except (ValueError, KeyError):
            continue
    return rows


def _parse_old(csv_text: str) -> list[dict]:
    # ponytail: spot here is the nearest-expiry index FUTURE's settle price,
    # not a true index close (pre-UDiFF bhavcopy has no underlying-price
    # column) - small basis vs real spot, upgrade to a joined index-bhavcopy
    # fetch if that noise ever matters more than "free and already in-file".
    reader = list(csv.DictReader(io.StringIO(csv_text)))
    nearest_future: dict[str, tuple[date, float]] = {}
    for row in reader:
        if row.get("INSTRUMENT") != "FUTIDX" or row.get("SYMBOL") not in SYMBOLS:
            continue
        try:
            expiry = datetime.strptime(row["EXPIRY_DT"].strip(), "%d-%b-%Y").date()
            settle = float(row["SETTLE_PR"])
        except (ValueError, KeyError):
            continue
        symbol = row["SYMBOL"]
        current = nearest_future.get(symbol)
        if current is None or expiry < current[0]:
            nearest_future[symbol] = (expiry, settle)

    rows = []
    for row in reader:
        symbol = row.get("SYMBOL")
        if symbol not in SYMBOLS or row.get("OPTION_TYP") not in ("CE", "PE"):
            continue
        spot_entry = nearest_future.get(symbol)
        if spot_entry is None:
            continue
        try:
            rows.append(dict(
                symbol=symbol,
                expiry=datetime.strptime(row["EXPIRY_DT"].strip(), "%d-%b-%Y").date(),
                strike=float(row["STRIKE_PR"]),
                option_type=row["OPTION_TYP"],
                settle=float(row["SETTLE_PR"]),
                spot=spot_entry[1],
                oi=int(float(row["OPEN_INT"])),
                volume=int(float(row["CONTRACTS"])),
            ))
        except (ValueError, KeyError):
            continue
    return rows


def _iv_row(d: date, row: dict) -> list | None:
    time_to_expiry = _time_to_expiry_years(d, row["expiry"])
    if time_to_expiry <= 0 or row["settle"] <= 0 or row["spot"] <= 0:
        return None
    option_type = OptionType.CALL if row["option_type"] == "CE" else OptionType.PUT
    iv = implied_volatility(
        option_type, row["settle"], row["spot"], row["strike"], DEFAULT_RISK_FREE_RATE, time_to_expiry
    )
    if iv is None:
        return None
    return [
        d.isoformat(), row["symbol"], row["expiry"].isoformat(), row["strike"], row["option_type"],
        row["settle"], row["spot"], round(time_to_expiry, 6), DEFAULT_RISK_FREE_RATE, round(iv, 6),
        row["oi"], row["volume"],
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--out", default=DEFAULT_OUT_PATH)
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    try:
        with open(args.out, "r") as f:
            file_exists = bool(f.readline())
    except FileNotFoundError:
        file_exists = False

    days_tried = days_with_data = rows_written = udiff_days = old_days = missing_days = 0

    with open(args.out, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "trade_date", "symbol", "expiry", "strike", "option_type",
                "settle_price", "spot", "time_to_expiry_years", "risk_free_rate", "implied_vol",
                "open_interest", "volume",
            ])

        d = start
        while d <= end:
            if d.weekday() < 5:  # Mon-Fri only; NSE holidays just 404/skip below, no holiday calendar needed
                days_tried += 1
                fetched = _fetch_day_csv(d)
                if fetched is None:
                    missing_days += 1
                else:
                    csv_text, fmt = fetched
                    udiff_days += fmt == "udiff"
                    old_days += fmt == "old"
                    parsed = _parse_udiff(csv_text) if fmt == "udiff" else _parse_old(csv_text)
                    day_rows = [r for raw in parsed if (r := _iv_row(d, raw)) is not None]
                    if day_rows:
                        writer.writerows(day_rows)
                        f.flush()
                        rows_written += len(day_rows)
                        days_with_data += 1
                time.sleep(_REQUEST_PACING_SECONDS)
                if days_tried % 50 == 0:
                    print(
                        f"{d}: {days_tried} days tried, {days_with_data} with data, {rows_written} rows, "
                        f"{missing_days} missing (holidays/gaps), {udiff_days} udiff / {old_days} old",
                        flush=True,
                    )
            d += timedelta(days=1)

    print(
        f"\nDone: {days_tried} weekdays tried ({start} to {end}), {days_with_data} had data, "
        f"{rows_written} IV rows written to {args.out}, {missing_days} missing (holidays/gaps), "
        f"{udiff_days} UDiFF / {old_days} pre-UDiFF format."
    )


if __name__ == "__main__":
    main()

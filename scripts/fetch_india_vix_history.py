#!/usr/bin/env python3
"""
Offline fetch of real historical India VIX daily candles via Angel One's
SmartAPI, into a CSV `core.analytics.benchmark.load_india_vix_series` can
read directly.

India VIX ("INDIA VIX", NSE) is an index, not a cash-equity - its scrip
master row has instrumenttype "AMXIDX", invisible to every existing
Angel One fetch path until `AngelOneMarketDataTransport.get_historical_candles`
gained `is_index=True` (see core/market_data/angelone_transport.py) to
resolve it via a separate cache. Same broker as every other historical
fetch in this project (Groww's market-data entitlement returns HTTP 403;
Angel One is this platform's designated historical-data source).

Daily bars, not intraday: India VIX is meaningful at daily resolution for
the cross-sectional/regime feature work this data source exists to
support (see docs/research/features/README.md) - no strategy in this
platform currently trades intraday off VIX directly.

    PYTHONPATH=. python scripts/fetch_india_vix_history.py [--out PATH] [--days N]

Requires the same ANGELONE_* credentials as every other Angel One fetch
script in this project.
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import date, timedelta

from apps.live_trader.config import load_angelone_credentials_from_env
from core.market_data.angelone_transport import (
    MAX_DAYS_PER_INTERVAL,
    AngelOneMarketDataCredentials,
    AngelOneMarketDataTransport,
    AngelOneMarketDataTransportConfig,
)
from core.market_data.errors import MarketDataTransportError

DEFAULT_OUT_PATH = "data/processed/nse_india_vix_daily.csv"
DEFAULT_DAYS = 3650  # ~10 years - matches the span of other real datasets in this project
SYMBOL = "INDIA VIX"
INTERVAL = "ONE_DAY"
_REQUEST_PACING_SECONDS = 0.4


def _date_chunks(from_date: date, to_date: date, max_days: int) -> list[tuple[date, date]]:
    chunks = []
    chunk_start = from_date
    while chunk_start <= to_date:
        chunk_end = min(chunk_start + timedelta(days=max_days - 1), to_date)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=DEFAULT_OUT_PATH)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()

    creds = load_angelone_credentials_from_env()
    transport = AngelOneMarketDataTransport(
        AngelOneMarketDataTransportConfig(
            credentials=AngelOneMarketDataCredentials(
                api_key=creds.api_key,
                client_code=creds.client_code,
                password=creds.password,
                totp_secret=creds.totp_secret,
            )
        )
    )

    to_date = date.today()
    from_date = to_date - timedelta(days=args.days)
    max_days_per_request = MAX_DAYS_PER_INTERVAL[INTERVAL] - 1  # stay strictly under the cap
    chunks = _date_chunks(from_date, to_date, max_days_per_request)
    print(f"Fetching {INTERVAL} India VIX candles, {from_date} to {to_date}, {len(chunks)} chunk(s)")

    rows_written = 0
    first_request = True
    failed: list[tuple[str, str]] = []

    tmp_out = f"{args.out}.tmp"
    with open(tmp_out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "exchange", "timestamp", "open", "high", "low", "close", "volume"])

        for chunk_start, chunk_end in chunks:
            if not first_request:
                time.sleep(_REQUEST_PACING_SECONDS)
            first_request = False

            from_str = chunk_start.strftime("%Y-%m-%d 09:15")
            to_str = chunk_end.strftime("%Y-%m-%d 15:30")
            try:
                candles = transport.get_historical_candles(
                    SYMBOL, "NSE", INTERVAL, from_str, to_str, is_index=True
                )
            except MarketDataTransportError as exc:
                failed.append((f"{chunk_start}..{chunk_end}", str(exc)))
                continue

            for candle in candles:
                timestamp_iso, open_, high, low, close, volume = candle
                timestamp = timestamp_iso[:19].replace("T", " ")
                writer.writerow([SYMBOL, "NSE", timestamp, open_, high, low, close, volume])
                rows_written += 1

    if rows_written:
        os.replace(tmp_out, args.out)
        print(f"\nWrote {rows_written} rows to {args.out}")
    else:
        os.remove(tmp_out)
        print(f"\nREFUSING to write {args.out}: 0 rows fetched - leaving any existing file untouched.")
    if failed:
        print(f"Failed chunks ({len(failed)}):")
        for window, reason in failed:
            print(f"  [{window}]: {reason}")


if __name__ == "__main__":
    main()

"""
Reads the cross-sectional daily-bar CSV (data/processed/nse_daily_12mo.csv)
used by the research feature-validation pipeline
(docs/research/features/README.md) - the same real dataset, reused here
for the dashboard's correlation/market-overview views rather than
fetching anything new.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

DEFAULT_DAILY_BARS_CSV = "data/processed/nse_daily_12mo.csv"


def load_closes_by_symbol(
    symbols: list[str] | None = None, csv_path: str | Path = DEFAULT_DAILY_BARS_CSV
) -> dict[str, list[tuple[str, float]]]:
    """
    Returns ``{symbol: [(date_str, close), ...]}`` in ascending date
    order, one entry per symbol present in the CSV (optionally filtered
    to ``symbols``). Raw ``csv.DictReader``, not pandas - matches
    ``core/analytics/benchmark.py::load_index_spot_series``'s own
    "plain csv.reader for speed" convention on this codebase's larger
    CSVs.
    """
    wanted = set(symbols) if symbols is not None else None
    by_symbol: dict[str, list[tuple[str, float]]] = defaultdict(list)

    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            symbol = row["symbol"]
            if wanted is not None and symbol not in wanted:
                continue
            by_symbol[symbol].append((row["timestamp"][:10], float(row["close"])))

    for series in by_symbol.values():
        series.sort(key=lambda pair: pair[0])

    return dict(by_symbol)


def available_symbols(csv_path: str | Path = DEFAULT_DAILY_BARS_CSV) -> list[str]:
    """Every symbol this CSV has data for - the universe correlation/breadth/gainers-losers can actually cover."""
    with open(csv_path, encoding="utf-8", newline="") as f:
        return sorted({row["symbol"] for row in csv.DictReader(f)})

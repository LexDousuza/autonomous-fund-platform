"""
Industry-benchmark comparison for backtest results.

``load_index_spot_series`` reads real NIFTY/BANKNIFTY spot prices straight
out of the options-IV history already backfilled to
``data/processed/nse_index_options_iv_history.csv`` (see
[[aqtp_options_iv_history_backfill_2026-07-26]] in project memory) - every
options row carries the underlying's spot price for that trade date, so a
plain buy-and-hold index series falls out of it for free without a second
data source.

``compute_return_series_metrics`` is deliberately generic over any
``(dates, values)`` equity-like series - it is used for both the index
benchmark (spot price) and the strategy portfolio (cumulative capital),
so both sides of a comparison are computed with exactly the same formulas.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

_CALENDAR_DAYS_PER_YEAR = 365.25
_TRADING_DAYS_PER_YEAR = 252


@dataclass(slots=True)
class ReturnSeriesMetrics:
    """
    Standard tearsheet stats for one equity-like value series.

    ``sharpe_ratio`` is annualized from the series' own periodic returns
    (assumed daily) against ``risk_free_rate`` - ``None`` if the series has
    fewer than 2 return observations or zero volatility. ``max_drawdown_pct``
    is the largest peak-to-trough decline as a fraction of the peak.
    """

    start_date: date
    end_date: date
    num_observations: int
    start_value: float
    end_value: float
    total_return_pct: float
    cagr_pct: float | None
    annualized_volatility_pct: float | None
    sharpe_ratio: float | None
    max_drawdown_pct: float


def compute_return_series_metrics(
    dates: list[date], values: list[float], risk_free_rate: float = 0.07
) -> ReturnSeriesMetrics:
    """
    ``dates``/``values`` must be the same length, sorted ascending by date,
    with ``values`` all-positive (a price or a capital level, never a raw
    PnL delta - callers with a PnL series must rebase to capital first, see
    ``apps/backtester/run_portfolio_backtest.py``).
    """
    if len(dates) != len(values):
        raise ValueError("dates and values must be the same length")
    if len(dates) < 2:
        raise ValueError("need at least 2 observations to compute return metrics")

    start_value, end_value = values[0], values[-1]
    total_return_pct = (end_value / start_value - 1.0) * 100.0

    elapsed_days = (dates[-1] - dates[0]).days
    cagr_pct = (
        ((end_value / start_value) ** (_CALENDAR_DAYS_PER_YEAR / elapsed_days) - 1.0) * 100.0
        if elapsed_days > 0 and start_value > 0
        else None
    )

    daily_returns = [
        values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] != 0
    ]
    annualized_volatility_pct = None
    sharpe_ratio = None
    if len(daily_returns) >= 2:
        mean_return = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_return) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        stdev = variance**0.5
        annualized_volatility_pct = stdev * (_TRADING_DAYS_PER_YEAR**0.5) * 100.0
        if stdev > 0:
            daily_risk_free = risk_free_rate / _TRADING_DAYS_PER_YEAR
            sharpe_ratio = ((mean_return - daily_risk_free) / stdev) * (_TRADING_DAYS_PER_YEAR**0.5)

    peak = values[0]
    max_drawdown_pct = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, (peak - value) / peak * 100.0)

    return ReturnSeriesMetrics(
        start_date=dates[0],
        end_date=dates[-1],
        num_observations=len(values),
        start_value=start_value,
        end_value=end_value,
        total_return_pct=total_return_pct,
        cagr_pct=cagr_pct,
        annualized_volatility_pct=annualized_volatility_pct,
        sharpe_ratio=sharpe_ratio,
        max_drawdown_pct=max_drawdown_pct,
    )


def load_index_spot_series(
    from_date: date,
    to_date: date,
    symbol: str = "NIFTY",
    csv_path: str | Path = "data/processed/nse_index_options_iv_history.csv",
) -> list[tuple[date, float]]:
    """
    One (date, spot_price) point per distinct trade_date in
    ``[from_date, to_date]`` for ``symbol`` ("NIFTY" or "BANKNIFTY") - every
    option row for a given underlying/date shares the same spot, so the
    first row seen per date is taken and the rest skipped.

    Raw ``csv.reader`` (not ``DictReader``) - this file is ~600MB/7.4M
    rows, and skipping the per-row dict construction for a column-index
    read matters at that size.
    """
    file_path = Path(csv_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Index history file not found: {file_path}")

    spot_by_date: dict[date, float] = {}
    with file_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        date_idx, symbol_idx, spot_idx = header.index("trade_date"), header.index("symbol"), header.index("spot")

        for row in reader:
            if row[symbol_idx] != symbol:
                continue
            trade_date = datetime.strptime(row[date_idx], "%Y-%m-%d").date()
            if trade_date < from_date or trade_date > to_date or trade_date in spot_by_date:
                continue
            spot_by_date[trade_date] = float(row[spot_idx])

    if not spot_by_date:
        raise ValueError(f"No {symbol} spot data found in {file_path} for {from_date}..{to_date}")

    return sorted(spot_by_date.items())


def load_india_vix_series(
    from_date: date,
    to_date: date,
    csv_path: str | Path = "data/processed/nse_india_vix_daily.csv",
) -> list[tuple[date, float]]:
    """
    One (date, close) point per distinct trading day in ``[from_date,
    to_date]`` from India VIX's own daily-candle CSV (see
    ``scripts/fetch_india_vix_history.py``) - same ``(date, value)``
    contract as ``load_index_spot_series``, so downstream analytics can
    treat it as an interchangeable series, but sourced from a dedicated
    fetch rather than backed out of the options-IV history file (unlike
    NIFTY/BANKNIFTY spot, India VIX isn't a byproduct of any options row).
    ``DictReader`` here, unlike ``load_index_spot_series`` - this file is a
    few thousand rows, not millions, so the per-row dict cost doesn't matter.
    """
    file_path = Path(csv_path)
    if not file_path.exists():
        raise FileNotFoundError(
            f"India VIX history file not found: {file_path} - run scripts/fetch_india_vix_history.py first"
        )

    close_by_date: dict[date, float] = {}
    with file_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            trade_date = datetime.strptime(row["timestamp"][:10], "%Y-%m-%d").date()
            if trade_date < from_date or trade_date > to_date:
                continue
            close_by_date[trade_date] = float(row["close"])

    if not close_by_date:
        raise ValueError(f"No India VIX data found in {file_path} for {from_date}..{to_date}")

    return sorted(close_by_date.items())

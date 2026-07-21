"""
App-level configuration loading for the paper trader.

Mirrors ``apps/live_trader/config.py``'s shape: env-var sourcing is an
application concern, not core trading logic, so this stays under
``apps/`` rather than ``core/``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta

from core.allocations.daily_mandate import RISK_APPETITES

_SUPPORTED_MARKET_DATA_PROVIDERS = {"memory", "groww"}
_DEFAULT_HISTORICAL_LOOKBACK_DAYS = 30
_DEFAULT_HISTORICAL_INTERVAL = "1day"  # real growwapi CANDLE_INTERVAL_DAY value


@dataclass(slots=True)
class PaperTraderConfig:
    """
    Fully validated, typed configuration for one paper-trader invocation.

    ``market_data_provider="memory"`` (the default) is the safe local/dev
    path: synthetic demo bars, no network, no credentials. ``"groww"``
    fetches real historical candles for capital/risk-aware bucket
    selection to run against - see
    ``apps/paper_trader/main.py:build_paper_session``. Bucket selection
    itself always runs, regardless of provider: it is a pure function of
    whatever price history is available (see
    ``core/market_data/bucket_selector.py``), synthetic or real.
    """

    market_data_provider: str
    intraday_capital_override: float | None
    risk_appetite_override: str | None
    historical_from_date: str
    historical_to_date: str
    historical_interval: str

    @property
    def requires_groww_credentials(self) -> bool:
        return self.market_data_provider == "groww"


def _parse_optional_float_env(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number if set, got: '{raw}'") from exc


def load_paper_trader_config_from_env() -> PaperTraderConfig:
    """
    Read, normalize, and validate the paper trader's top-level session
    configuration from environment variables.

    Environment variables:

    - ``PAPER_MARKET_DATA_PROVIDER``: ``"memory"`` (default) or ``"groww"``.
    - ``PAPER_INTRADAY_CAPITAL``: optional override for the mandate's
      ``buckets.intraday_bucket`` - the capital driving bucket selection
      and position sizing for this run only (the mandate file itself is
      untouched).
    - ``PAPER_RISK_APPETITE``: optional override for the mandate's
      ``risk_appetite`` (``"conservative"``/``"moderate"``/``"aggressive"``).
    - ``PAPER_HISTORICAL_FROM_DATE`` / ``PAPER_HISTORICAL_TO_DATE``:
      ``"YYYY-MM-DD"`` window for Groww historical candles (only used
      when ``market_data_provider="groww"``). Defaults to the last
      30 days ending today.
    - ``PAPER_HISTORICAL_INTERVAL``: candle interval for Groww historical
      candles (default ``"1day"``; must be a real growwapi candle-interval
      string, e.g. ``"1day"``, ``"5minute"``).

    Raises ``ValueError`` for an unsupported provider or risk appetite,
    or a non-numeric ``PAPER_INTRADAY_CAPITAL``.
    """
    market_data_provider = os.environ.get("PAPER_MARKET_DATA_PROVIDER", "memory").strip().lower()
    if market_data_provider not in _SUPPORTED_MARKET_DATA_PROVIDERS:
        raise ValueError(
            f"Unsupported PAPER_MARKET_DATA_PROVIDER '{market_data_provider}'. "
            f"Supported: {sorted(_SUPPORTED_MARKET_DATA_PROVIDERS)}"
        )

    intraday_capital_override = _parse_optional_float_env("PAPER_INTRADAY_CAPITAL")

    risk_appetite_override = os.environ.get("PAPER_RISK_APPETITE", "").strip().lower() or None
    if risk_appetite_override is not None and risk_appetite_override not in RISK_APPETITES:
        raise ValueError(
            f"Unsupported PAPER_RISK_APPETITE '{risk_appetite_override}'. "
            f"Supported: {sorted(RISK_APPETITES)}"
        )

    today = date.today()
    default_from_date = today - timedelta(days=_DEFAULT_HISTORICAL_LOOKBACK_DAYS)
    historical_from_date = (
        os.environ.get("PAPER_HISTORICAL_FROM_DATE", "").strip() or default_from_date.isoformat()
    )
    historical_to_date = os.environ.get("PAPER_HISTORICAL_TO_DATE", "").strip() or today.isoformat()
    historical_interval = (
        os.environ.get("PAPER_HISTORICAL_INTERVAL", "").strip() or _DEFAULT_HISTORICAL_INTERVAL
    )

    return PaperTraderConfig(
        market_data_provider=market_data_provider,
        intraday_capital_override=intraday_capital_override,
        risk_appetite_override=risk_appetite_override,
        historical_from_date=historical_from_date,
        historical_to_date=historical_to_date,
        historical_interval=historical_interval,
    )

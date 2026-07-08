"""
App-level configuration loading for the live trader.

Credential sourcing (today: environment variables; tomorrow, maybe a
secrets manager) is an application concern, not core trading logic, so
this stays under ``apps/`` rather than ``core/``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class MissingGrowwCredentialsError(RuntimeError):
    """Raised when required Groww credentials are missing from the environment."""


@dataclass(slots=True)
class GrowwEnvCredentials:
    """Groww credentials as read from the process environment."""

    api_key: str
    access_token: str
    base_url: str = ""


def load_groww_credentials_from_env() -> GrowwEnvCredentials:
    """
    Read Groww broker credentials from environment variables.

    Required: ``GROWW_API_KEY``, ``GROWW_ACCESS_TOKEN``.
    Optional: ``GROWW_BASE_URL``.

    Raises ``MissingGrowwCredentialsError`` immediately if a required
    variable is missing or blank, so the live trader fails fast instead
    of starting a session with bad credentials or silently falling back
    to paper mode.
    """
    api_key = os.environ.get("GROWW_API_KEY", "").strip()
    access_token = os.environ.get("GROWW_ACCESS_TOKEN", "").strip()
    base_url = os.environ.get("GROWW_BASE_URL", "").strip()

    missing = [
        name
        for name, value in (("GROWW_API_KEY", api_key), ("GROWW_ACCESS_TOKEN", access_token))
        if not value
    ]

    if missing:
        raise MissingGrowwCredentialsError(
            "Missing required Groww credentials in environment: "
            f"{', '.join(missing)}. Set these before running the live trader."
        )

    return GrowwEnvCredentials(api_key=api_key, access_token=access_token, base_url=base_url)


def load_market_data_provider_from_env() -> str:
    """
    Read which market-data provider the live trader should use.

    Defaults to ``"memory"`` - the safe local/dev path (deterministic
    demo bars, no network) - unless ``LIVE_MARKET_DATA_PROVIDER`` is
    explicitly set (e.g. to ``"groww"`` for real live data). This is a
    deliberate, visible configuration choice rather than a hidden
    fallback: real live market data is opt-in, never assumed.
    """
    return os.environ.get("LIVE_MARKET_DATA_PROVIDER", "memory").strip().lower()

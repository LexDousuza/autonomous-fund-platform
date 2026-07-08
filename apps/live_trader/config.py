"""
App-level configuration loading for the live trader.

Credential sourcing (today: environment variables; tomorrow, maybe a
secrets manager) is an application concern, not core trading logic, so
this stays under ``apps/`` rather than ``core/``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

LIVE_CONFIRM_ENV_VAR = "LIVE_CONFIRM"
LIVE_CONFIRM_REQUIRED_VALUE = "I_UNDERSTAND_THIS_CAN_PLACE_REAL_ORDERS"

_SUPPORTED_BROKER_PROVIDERS = {"paper", "groww"}
_SUPPORTED_MARKET_DATA_PROVIDERS = {"memory", "groww"}

# Explicitly the only (broker, market_data) pairs this app will run.
# "groww broker + memory data" is deliberately excluded: placing real
# orders off synthetic demo bars is nonsensical and dangerous, not a
# safety mode worth supporting.
_ALLOWED_PROVIDER_COMBINATIONS = {
    ("paper", "memory"),
    ("paper", "groww"),
    ("groww", "groww"),
}


class MissingGrowwCredentialsError(RuntimeError):
    """Raised when required Groww credentials are missing from the environment."""


class LiveTradingNotConfirmedError(RuntimeError):
    """
    Raised when the live trader is configured to use a real Groww live
    broker but the required explicit confirmation env var is missing or
    doesn't match exactly.

    This is a deliberate, hard safety gate distinct from ordinary config
    validation errors: paper-broker sessions (regardless of market-data
    provider) never need it, and no amount of "the rest of the config
    looks fine" substitutes for it.
    """


@dataclass(slots=True)
class GrowwEnvCredentials:
    """Groww credentials as read from the process environment."""

    api_key: str
    access_token: str
    base_url: str = ""


@dataclass(slots=True)
class LiveTraderConfig:
    """
    Fully validated, typed configuration for one live-trader invocation.

    Built by ``load_live_trader_config_from_env()``, which normalizes
    (case/whitespace), validates allowed values and provider/mode
    combinations, and enforces the live-confirmation safety gate - all
    before any credential is read or any factory is called. Everything
    downstream (``build_live_session``) trusts this object completely.

    ``broker_mode`` is derived from ``broker_provider``, not
    independently configurable: ``"paper"`` always means
    ``mode="paper"``, ``"groww"`` always means ``mode="live"``. This
    removes an entire class of misconfiguration - e.g. an operator
    setting a mode env var to "paper" while the provider is "groww",
    which would *look* safe but isn't, since Groww has no verified
    separate paper API (see ``core/broker/groww_adapter.py``).
    """

    broker_provider: str
    broker_mode: str
    market_data_provider: str
    universe_exchange: str
    dry_run: bool
    live_confirmed: bool
    max_bars: int | None = None

    @property
    def requires_groww_credentials(self) -> bool:
        """True if either the broker or the market-data feed needs a real Groww connection."""
        return self.broker_provider == "groww" or self.market_data_provider == "groww"

    @property
    def requires_live_confirmation(self) -> bool:
        """True only for a real Groww live broker - never for paper, regardless of market data."""
        return self.broker_provider == "groww"


def load_groww_credentials_from_env() -> GrowwEnvCredentials:
    """
    Read Groww credentials from environment variables.

    Required: ``GROWW_API_KEY``, ``GROWW_ACCESS_TOKEN``.
    Optional: ``GROWW_BASE_URL``.

    Raises ``MissingGrowwCredentialsError`` immediately if a required
    variable is missing or blank. Callers are responsible for only
    calling this when a Groww connection is actually needed (see
    ``LiveTraderConfig.requires_groww_credentials``) - this function
    itself doesn't know or care why it was called.
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


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_optional_int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer if set, got: '{raw}'") from exc


def _is_live_confirmed() -> bool:
    return os.environ.get(LIVE_CONFIRM_ENV_VAR, "").strip() == LIVE_CONFIRM_REQUIRED_VALUE


def load_live_trader_config_from_env() -> LiveTraderConfig:
    """
    Read, normalize, and validate the live trader's top-level session
    configuration from environment variables. This is always the first
    thing the live trader does - before loading the mandate, the
    universe, or any credential - so a bad or unconfirmed configuration
    is rejected as fast as possible.

    Environment variables:

    - ``LIVE_BROKER_PROVIDER``: ``"paper"`` (default) or ``"groww"``.
    - ``LIVE_MARKET_DATA_PROVIDER``: ``"memory"`` (default) or ``"groww"``.
    - ``LIVE_UNIVERSE_EXCHANGE``: which exchange to select instruments
      from (default ``"NSE"``).
    - ``LIVE_DRY_RUN``: ``"1"``/``"true"``/``"yes"``/``"on"``
      (case-insensitive) to build and fully validate a session without
      actually running it (default ``False``).
    - ``LIVE_MAX_BARS``: optional integer cap on how many bars the Groww
      live market-data feed polls before stopping; ignored for other
      providers.
    - ``LIVE_CONFIRM``: must exactly equal
      ``"I_UNDERSTAND_THIS_CAN_PLACE_REAL_ORDERS"`` when
      ``LIVE_BROKER_PROVIDER=groww``; irrelevant otherwise.

    Raises ``ValueError`` for an unsupported provider value, a
    non-integer ``LIVE_MAX_BARS``, or a provider combination outside
    ``paper+memory`` / ``paper+groww`` / ``groww+groww``.
    Raises ``LiveTradingNotConfirmedError`` if a real Groww live broker
    is requested without the exact confirmation string.
    """
    broker_provider = os.environ.get("LIVE_BROKER_PROVIDER", "paper").strip().lower()
    market_data_provider = os.environ.get("LIVE_MARKET_DATA_PROVIDER", "memory").strip().lower()
    universe_exchange = os.environ.get("LIVE_UNIVERSE_EXCHANGE", "NSE").strip().upper()
    dry_run = _parse_bool_env("LIVE_DRY_RUN", default=False)
    max_bars = _parse_optional_int_env("LIVE_MAX_BARS")

    if broker_provider not in _SUPPORTED_BROKER_PROVIDERS:
        raise ValueError(
            f"Unsupported LIVE_BROKER_PROVIDER '{broker_provider}'. "
            f"Supported: {sorted(_SUPPORTED_BROKER_PROVIDERS)}"
        )
    if market_data_provider not in _SUPPORTED_MARKET_DATA_PROVIDERS:
        raise ValueError(
            f"Unsupported LIVE_MARKET_DATA_PROVIDER '{market_data_provider}'. "
            f"Supported: {sorted(_SUPPORTED_MARKET_DATA_PROVIDERS)}"
        )
    if (broker_provider, market_data_provider) not in _ALLOWED_PROVIDER_COMBINATIONS:
        raise ValueError(
            f"Unsupported broker/market-data combination: broker='{broker_provider}', "
            f"market_data='{market_data_provider}'. Supported combinations: "
            f"{sorted(_ALLOWED_PROVIDER_COMBINATIONS)}"
        )

    config = LiveTraderConfig(
        broker_provider=broker_provider,
        broker_mode="live" if broker_provider == "groww" else "paper",
        market_data_provider=market_data_provider,
        universe_exchange=universe_exchange,
        dry_run=dry_run,
        live_confirmed=_is_live_confirmed(),
        max_bars=max_bars,
    )

    if config.requires_live_confirmation and not config.live_confirmed:
        raise LiveTradingNotConfirmedError(
            "Refusing to start: LIVE_BROKER_PROVIDER='groww' requests a real live "
            "broker, which can place real orders with real capital. Set "
            f"{LIVE_CONFIRM_ENV_VAR}={LIVE_CONFIRM_REQUIRED_VALUE} to confirm you "
            "understand this, then run again."
        )

    return config

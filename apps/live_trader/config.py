"""
App-level configuration loading for the live trader.

Credential sourcing (today: environment variables; tomorrow, maybe a
secrets manager) is an application concern, not core trading logic, so
this stays under ``apps/`` rather than ``core/``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta

LIVE_CONFIRM_ENV_VAR = "LIVE_CONFIRM"
LIVE_CONFIRM_REQUIRED_VALUE = "I_UNDERSTAND_THIS_CAN_PLACE_REAL_ORDERS"

_SUPPORTED_BROKER_PROVIDERS = {"paper", "groww", "kite"}
_SUPPORTED_MARKET_DATA_PROVIDERS = {"memory", "groww", "kite"}

# Lookback window for the candidate price history bucket selection runs
# against (see build_live_session) - short on purpose, this only needs to
# be enough bars for a same-day volatility/affordability read, not a
# strategy backtest window. Mirrors apps/paper_trader/config.py's
# equivalent constants.
_DEFAULT_HISTORICAL_LOOKBACK_DAYS = 10
_DEFAULT_HISTORICAL_INTERVAL = "1day"  # real growwapi CANDLE_INTERVAL_DAY value
_DEFAULT_KITE_HISTORICAL_INTERVAL = "day"  # real kiteconnect candle-interval value - NOT "1day"

# Explicitly the only (broker, market_data) pairs this app will run.
# "groww/kite broker + memory data" is deliberately excluded: placing
# real orders off synthetic demo bars is nonsensical and dangerous, not a
# safety mode worth supporting. "kite broker + groww data" IS supported
# (unlike Groww, Kite's order/account APIs are free on its Personal tier -
# only Kite's own market data requires the paid Connect tier - so
# real Kite order execution paired with Groww-sourced quotes is a
# legitimate, cost-motivated combination, not a mismatched one).
_ALLOWED_PROVIDER_COMBINATIONS = {
    ("paper", "memory"),
    ("paper", "groww"),
    ("paper", "kite"),
    ("groww", "groww"),
    ("kite", "kite"),
    ("kite", "groww"),
}

# Broker providers that place real orders against a real brokerage
# account - as opposed to "paper", which only ever simulates fills
# locally (see ``PaperBroker``). Both require the live-confirmation
# safety gate below and both need their own real credentials.
_LIVE_BROKER_PROVIDERS = {"groww", "kite"}


class MissingGrowwCredentialsError(RuntimeError):
    """Raised when required Groww credentials are missing from the environment."""


class MissingKiteCredentialsError(RuntimeError):
    """Raised when required Kite credentials are missing from the environment."""


class LiveTradingNotConfirmedError(RuntimeError):
    """
    Raised when the live trader is configured to use a real live broker
    (Groww or Kite) but the required explicit confirmation env var is
    missing or doesn't match exactly.

    This is a deliberate, hard safety gate distinct from ordinary config
    validation errors: paper-broker sessions (regardless of market-data
    provider) never need it, and no amount of "the rest of the config
    looks fine" substitutes for it.
    """


@dataclass(slots=True)
class GrowwEnvCredentials:
    """
    Groww credentials as read from the process environment.

    ``api_key`` + ``secret`` are the two values Groww's dashboard
    actually generates together (API Key + API Secret) - verified
    against https://groww.in/trade-api/docs/python-sdk. They are
    exchanged for a short-lived access token at broker/feed construction
    time (see ``core/broker/groww_transport.py``), not supplied directly.
    """

    api_key: str
    secret: str


@dataclass(slots=True)
class KiteEnvCredentials:
    """
    Kite Connect credentials as read from the process environment.

    ``api_key`` + ``access_token`` - unlike Groww's api_key+secret (a
    static pair exchanged for a token on every call), Kite's
    ``access_token`` is itself the day's already-issued credential:
    obtained via a one-time browser login flow, valid until 6 AM IST the
    next day, and only ever produced by
    ``scripts/kite_generate_access_token.py`` (run manually by the
    operator each morning) - never by this platform itself. See
    ``core/broker/kite_models.py``'s ``KiteCredentials`` docstring.
    """

    api_key: str
    access_token: str


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
    ``mode="paper"``, ``"groww"``/``"kite"`` always mean ``mode="live"``.
    This removes an entire class of misconfiguration - e.g. an operator
    setting a mode env var to "paper" while the provider is "groww"/"kite",
    which would *look* safe but isn't, since neither broker has a
    verified separate paper API (see ``core/broker/groww_adapter.py``/
    ``core/broker/kite_adapter.py``).
    """

    broker_provider: str
    broker_mode: str
    market_data_provider: str
    universe_exchange: str
    dry_run: bool
    live_confirmed: bool
    allow_unreconciled_positions: bool
    allow_unclean_prior_session: bool
    historical_from_date: str
    historical_to_date: str
    historical_interval: str
    max_bars: int | None = None

    @property
    def requires_groww_credentials(self) -> bool:
        """True if either the broker or the market-data feed needs a real Groww connection."""
        return self.broker_provider == "groww" or self.market_data_provider == "groww"

    @property
    def requires_kite_credentials(self) -> bool:
        """True if either the broker or the market-data feed needs a real Kite connection."""
        return self.broker_provider == "kite" or self.market_data_provider == "kite"

    @property
    def requires_live_confirmation(self) -> bool:
        """True only for a real live broker (Groww or Kite) - never for paper, regardless of market data."""
        return self.broker_provider in _LIVE_BROKER_PROVIDERS


def load_groww_credentials_from_env() -> GrowwEnvCredentials:
    """
    Read Groww credentials from environment variables.

    Required: ``GROWW_API_KEY``, ``GROWW_API_SECRET`` - the API Key and
    API Secret Groww's dashboard generates together (see
    ``GrowwEnvCredentials``'s docstring), not a long-lived access token.

    Raises ``MissingGrowwCredentialsError`` immediately if a required
    variable is missing or blank. Callers are responsible for only
    calling this when a Groww connection is actually needed (see
    ``LiveTraderConfig.requires_groww_credentials``) - this function
    itself doesn't know or care why it was called.
    """
    api_key = os.environ.get("GROWW_API_KEY", "").strip()
    secret = os.environ.get("GROWW_API_SECRET", "").strip()

    missing = [
        name
        for name, value in (("GROWW_API_KEY", api_key), ("GROWW_API_SECRET", secret))
        if not value
    ]

    if missing:
        raise MissingGrowwCredentialsError(
            "Missing required Groww credentials in environment: "
            f"{', '.join(missing)}. Set these before running the live trader."
        )

    return GrowwEnvCredentials(api_key=api_key, secret=secret)


def load_kite_credentials_from_env() -> KiteEnvCredentials:
    """
    Read Kite credentials from environment variables.

    Required: ``KITE_API_KEY``, ``KITE_ACCESS_TOKEN`` - the latter is the
    day's already-issued access token (see ``KiteEnvCredentials``'s
    docstring), never a password or long-lived secret. Obtain it by
    running ``scripts/kite_generate_access_token.py`` each morning before
    starting a Kite-backed session; it expires at 6 AM IST the next day.

    Raises ``MissingKiteCredentialsError`` immediately if a required
    variable is missing or blank. Callers are responsible for only
    calling this when a Kite connection is actually needed (see
    ``LiveTraderConfig.requires_kite_credentials``).
    """
    api_key = os.environ.get("KITE_API_KEY", "").strip()
    access_token = os.environ.get("KITE_ACCESS_TOKEN", "").strip()

    missing = [
        name
        for name, value in (("KITE_API_KEY", api_key), ("KITE_ACCESS_TOKEN", access_token))
        if not value
    ]

    if missing:
        raise MissingKiteCredentialsError(
            "Missing required Kite credentials in environment: "
            f"{', '.join(missing)}. Set these before running the live trader - "
            "run scripts/kite_generate_access_token.py to obtain today's KITE_ACCESS_TOKEN."
        )

    return KiteEnvCredentials(api_key=api_key, access_token=access_token)


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

    - ``LIVE_BROKER_PROVIDER``: ``"paper"`` (default), ``"groww"``, or ``"kite"``.
    - ``LIVE_MARKET_DATA_PROVIDER``: ``"memory"`` (default), ``"groww"``, or ``"kite"``.
    - ``LIVE_UNIVERSE_EXCHANGE``: which exchange to select instruments
      from (default ``"NSE"``).
    - ``LIVE_DRY_RUN``: ``"1"``/``"true"``/``"yes"``/``"on"``
      (case-insensitive) to build and fully validate a session without
      actually running it (default ``False``).
    - ``LIVE_MAX_BARS``: optional integer cap on how many bars a Groww/
      Kite live market-data feed polls before stopping; ignored for
      other providers.
    - ``LIVE_CONFIRM``: must exactly equal
      ``"I_UNDERSTAND_THIS_CAN_PLACE_REAL_ORDERS"`` when
      ``LIVE_BROKER_PROVIDER`` is ``"groww"`` or ``"kite"``; irrelevant
      otherwise.
    - ``LIVE_ALLOW_UNRECONCILED_POSITIONS``: ``"1"``/``"true"``/``"yes"``/
      ``"on"`` (case-insensitive) to proceed even if the broker reports
      open positions before this session has placed anything (default
      ``False`` - see ``core/execution/reconciliation.py`` and
      ``build_live_session``'s reconciliation check).
    - ``LIVE_ALLOW_UNCLEAN_PRIOR_SESSION``: ``"1"``/``"true"``/``"yes"``/
      ``"on"`` (case-insensitive) to proceed even if the previous session
      never reached a terminal status - i.e. the process likely crashed
      hard (default ``False`` - see
      ``core/audit/file_sink.find_unclean_prior_session``).
    - ``LIVE_HISTORICAL_FROM_DATE`` / ``LIVE_HISTORICAL_TO_DATE``:
      ``"YYYY-MM-DD"`` window for the Groww/Kite historical candles
      bucket selection reads price history from (only used when
      ``market_data_provider`` is ``"groww"``/``"kite"``; see
      ``build_live_session``'s candidate bucket-selection step). Defaults
      to the last 10 days ending today.
    - ``LIVE_HISTORICAL_INTERVAL``: candle interval for that same
      historical fetch (default ``"1day"`` for Groww, ``"day"`` for
      Kite - see ``_build_candidate_market_data_config``).

    Raises ``ValueError`` for an unsupported provider value, a
    non-integer ``LIVE_MAX_BARS``, or a provider combination outside
    ``paper+memory`` / ``paper+groww`` / ``paper+kite`` / ``groww+groww``
    / ``kite+kite``.
    Raises ``LiveTradingNotConfirmedError`` if a real live broker (Groww
    or Kite) is requested without the exact confirmation string.
    """
    broker_provider = os.environ.get("LIVE_BROKER_PROVIDER", "paper").strip().lower()
    market_data_provider = os.environ.get("LIVE_MARKET_DATA_PROVIDER", "memory").strip().lower()
    universe_exchange = os.environ.get("LIVE_UNIVERSE_EXCHANGE", "NSE").strip().upper()
    dry_run = _parse_bool_env("LIVE_DRY_RUN", default=False)
    allow_unreconciled_positions = _parse_bool_env("LIVE_ALLOW_UNRECONCILED_POSITIONS", default=False)
    allow_unclean_prior_session = _parse_bool_env("LIVE_ALLOW_UNCLEAN_PRIOR_SESSION", default=False)
    max_bars = _parse_optional_int_env("LIVE_MAX_BARS")

    today = date.today()
    default_from_date = today - timedelta(days=_DEFAULT_HISTORICAL_LOOKBACK_DAYS)
    historical_from_date = (
        os.environ.get("LIVE_HISTORICAL_FROM_DATE", "").strip() or default_from_date.isoformat()
    )
    historical_to_date = os.environ.get("LIVE_HISTORICAL_TO_DATE", "").strip() or today.isoformat()
    # Default interval string depends on which provider will actually
    # consume it - Kite's candle-interval vocabulary ("day") differs from
    # Groww's ("1day"); an explicit LIVE_HISTORICAL_INTERVAL always wins.
    default_historical_interval = (
        _DEFAULT_KITE_HISTORICAL_INTERVAL if market_data_provider == "kite" else _DEFAULT_HISTORICAL_INTERVAL
    )
    historical_interval = (
        os.environ.get("LIVE_HISTORICAL_INTERVAL", "").strip() or default_historical_interval
    )

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
        broker_mode="live" if broker_provider in _LIVE_BROKER_PROVIDERS else "paper",
        market_data_provider=market_data_provider,
        universe_exchange=universe_exchange,
        dry_run=dry_run,
        live_confirmed=_is_live_confirmed(),
        allow_unreconciled_positions=allow_unreconciled_positions,
        allow_unclean_prior_session=allow_unclean_prior_session,
        historical_from_date=historical_from_date,
        historical_to_date=historical_to_date,
        historical_interval=historical_interval,
        max_bars=max_bars,
    )

    if config.requires_live_confirmation and not config.live_confirmed:
        raise LiveTradingNotConfirmedError(
            f"Refusing to start: LIVE_BROKER_PROVIDER='{broker_provider}' requests a "
            "real live broker, which can place real orders with real capital. Set "
            f"{LIVE_CONFIRM_ENV_VAR}={LIVE_CONFIRM_REQUIRED_VALUE} to confirm you "
            "understand this, then run again."
        )

    return config

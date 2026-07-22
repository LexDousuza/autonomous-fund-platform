"""
Angel One (SmartAPI) market-data transport - historical candle data only.

Angel One is deliberately scoped to historical data in this platform;
Kite remains the only live/paper trading broker (see
``core/market_data/factory.py``'s ``"angelone"`` branch, which refuses
``mode="live"``/``mode="paper"`` outright). This module has no order
placement and is never wired into ``core/broker/``.

Wraps the official ``smartapi-python`` SDK (``SmartApi.SmartConnect``).
Verified against the installed SDK's own source
(``generateSession``/``_request``/``getCandleData`` in
``SmartApi/smartConnect.py``) and the official docs
(https://smartapi.angelbroking.com/docs/Historical) - not guessed:

- ``generateSession(client_code, password, totp)`` does NOT raise on a
  plain ``status: False`` login failure (wrong password/TOTP/client
  code) - it just returns the failed response dict as-is. It DOES raise
  a real ``SmartApi.smartExceptions`` exception when the response
  additionally carries an ``error_type`` field. Both paths are
  translated into this platform's shared ``core.market_data.errors``
  hierarchy here so callers never have to know the difference.
- ``getCandleData``'s successful response shape is
  ``{"status": true, "message": "SUCCESS", "errorcode": "", "data": [[
  timestamp_iso, open, high, low, close, volume], ...]}`` - a positional
  array per candle, like Groww's, not Kite's already-keyed dicts.
- Angel One enforces an explicit max-days-per-request ceiling per
  interval (``MAX_DAYS_PER_INTERVAL``, verified from the docs page) - a
  too-wide request is rejected here before ever reaching the API,
  matching ``kite_historical_feed.py``/``groww_historical_feed.py``'s
  "fail loudly rather than silently return truncated data" stance.
- Unlike Kite (a numeric ``instrument_token`` from ``instruments()``),
  Angel One needs a ``symboltoken`` resolved from its public scrip
  master JSON dump (``_SCRIP_MASTER_URL``, not part of the SDK - a
  separate static file), matched on ``name``/``exch_seg`` with
  ``instrumenttype == ""`` for plain cash-equity entries (verified by
  fetching and inspecting the real file: e.g. RELIANCE on NSE is
  ``{"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE",
  "instrumenttype": "", "exch_seg": "NSE", ...}``).
- The SDK requires several device-identifying headers
  (``X-ClientLocalIP``/``X-ClientPublicIP``/``X-MACAddress``) that it
  fills with its own hardcoded fallback values regardless of the real
  machine (confirmed in the SDK's own source: a ``try/finally`` always
  overwrites the fetched public IP with a fixed fallback) - these are
  not meaningfully validated, so no attempt is made here to supply real
  ones.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any

from SmartApi import SmartConnect
from SmartApi.smartExceptions import PermissionException, TokenException

from core.market_data.errors import (
    MarketDataAuthenticationError,
    MarketDataProviderError,
    MarketDataTransportError,
    UnsupportedSymbolError,
)
from core.utils.resilience import CircuitBreaker, CircuitOpenError, RetryPolicy, retry_with_backoff
from core.utils.totp import generate_totp

_RETRY_POLICY = RetryPolicy()

# Public, unauthenticated instrument master - not part of the SmartAPI
# SDK itself. ~35MB / 160k+ rows across all exchanges/segments; fetched
# and cached once per transport instance, same reasoning as
# KiteMarketDataTransport._resolve_instrument_token.
_SCRIP_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
_SCRIP_MASTER_TIMEOUT_SECONDS = 30.0

# Verified against https://smartapi.angelbroking.com/docs/Historical.
MAX_DAYS_PER_INTERVAL = {
    "ONE_MINUTE": 30,
    "THREE_MINUTE": 60,
    "FIVE_MINUTE": 100,
    "TEN_MINUTE": 100,
    "FIFTEEN_MINUTE": 200,
    "THIRTY_MINUTE": 200,
    "ONE_HOUR": 400,
    "ONE_DAY": 2000,
}


def _is_retryable(exc: Exception) -> bool:
    """Mirrors KiteMarketDataTransport/GrowwMarketDataTransport's identical reasoning."""
    if isinstance(exc, MarketDataAuthenticationError):
        return False
    if isinstance(exc, MarketDataProviderError):
        return exc.status_code is None or exc.status_code >= 500 or exc.status_code == 429
    return isinstance(exc, MarketDataTransportError)


def _translate_exception(exc: Exception, action: str) -> MarketDataTransportError:
    """Map a raised ``SmartApi.smartExceptions`` exception onto this platform's shared hierarchy."""
    if isinstance(exc, (TokenException, PermissionException)):
        return MarketDataAuthenticationError(
            f"Angel One market data authentication failed during {action}: {exc}",
            status_code=getattr(exc, "code", None),
        )
    code = getattr(exc, "code", None)
    if code is not None:
        return MarketDataProviderError(
            f"Angel One market data API returned an error during {action}: {exc}", status_code=code
        )
    return MarketDataTransportError(f"Angel One market data request failed during {action}: {exc}")


def _raise_if_status_false(response: Any, action: str, *, is_auth: bool) -> dict[str, Any]:
    """
    ``generateSession``/``getCandleData`` return a plain ``{"status":
    False, ...}`` dict rather than raising for a login/candle-request
    failure with no ``error_type`` (see module docstring) - this is the
    other half of error translation, alongside ``_translate_exception``
    for the path that does raise.
    """
    if not isinstance(response, dict):
        raise MarketDataTransportError(
            f"Angel One market data {action} returned an unexpected response type: {response!r}"
        )
    if response.get("status"):
        return response

    message = response.get("message") or "unknown error"
    errorcode = response.get("errorcode") or ""
    detail = f"Angel One market data {action} failed: {message} (errorcode={errorcode!r})"
    if is_auth:
        raise MarketDataAuthenticationError(detail)
    raise MarketDataProviderError(detail)


@dataclass(frozen=True)
class AngelOneMarketDataCredentials:
    """
    SmartAPI credentials as read from the environment (see
    ``apps/live_trader/config.py``'s ``AngelOneEnvCredentials``).

    ``totp_secret`` is a base32 TOTP seed, not a static code - a live
    6-digit code is computed from it at session-generation time (see
    ``core/utils/totp.py``), the same shape as Groww's TOTP-based login
    (``core/broker/groww_transport.py``). Unlike Kite Connect, this
    entire login flow is headless/automatable - no browser redirect or
    daily manual step is needed.
    """

    api_key: str
    client_code: str
    password: str
    totp_secret: str


@dataclass(frozen=True)
class AngelOneMarketDataTransportConfig:
    credentials: AngelOneMarketDataCredentials
    timeout_seconds: float = 10.0


class AngelOneMarketDataTransport:
    """
    Thin wrapper around ``SmartApi.SmartConnect`` for historical-candle
    reads only. Every public method sends one request and returns plain
    ``dict``/``list`` data, matching ``KiteMarketDataTransport``'s
    contract.
    """

    def __init__(
        self, config: AngelOneMarketDataTransportConfig, client: SmartConnect | None = None
    ) -> None:
        self._config = config
        self._circuit_breaker = CircuitBreaker()
        self._client = client if client is not None else self._authenticate(config)
        self._symbol_token_cache: dict[tuple[str, str], str] = {}

    def _authenticate(self, config: AngelOneMarketDataTransportConfig) -> SmartConnect:
        def do_auth() -> SmartConnect:
            client = SmartConnect(api_key=config.credentials.api_key, timeout=config.timeout_seconds)
            totp = generate_totp(config.credentials.totp_secret)
            try:
                response = client.generateSession(
                    config.credentials.client_code, config.credentials.password, totp
                )
            except Exception as exc:  # noqa: BLE001 - translated into our own hierarchy below
                raise _translate_exception(exc, "authentication") from exc
            _raise_if_status_false(response, "authentication", is_auth=True)
            return client

        return retry_with_backoff(do_auth, policy=_RETRY_POLICY, is_retryable=_is_retryable)

    def _call(self, action: str, fn) -> Any:
        def guarded() -> Any:
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                raise _translate_exception(exc, action) from exc

        try:
            return self._circuit_breaker.call(
                lambda: retry_with_backoff(guarded, policy=_RETRY_POLICY, is_retryable=_is_retryable)
            )
        except CircuitOpenError as exc:
            raise MarketDataTransportError(
                f"Angel One market data circuit open, refusing {action}: {exc}"
            ) from exc

    # -- public operations -------------------------------------------

    def get_historical_candles(
        self, symbol: str, exchange: str, interval: str, from_date: str, to_date: str
    ) -> list[list[Any]]:
        """
        Fetch historical OHLCV candles for ``symbol`` on ``exchange``.

        ``interval`` must be one of ``MAX_DAYS_PER_INTERVAL``'s keys;
        ``from_date``/``to_date`` must be ``"yyyy-MM-dd HH:mm"`` (Angel
        One's required format, verified from the docs). Resolves
        ``symbol``/``exchange`` to a ``symboltoken`` via the scrip master
        first (required by ``getCandleData``).
        """
        symbol_token = self._resolve_symbol_token(symbol, exchange)
        params = {
            "exchange": exchange,
            "symboltoken": symbol_token,
            "interval": interval,
            "fromdate": from_date,
            "todate": to_date,
        }
        response = self._call("get_historical_candles", lambda: self._client.getCandleData(params))
        response = _raise_if_status_false(response, "get_historical_candles", is_auth=False)
        return response.get("data") or []

    # -- internals -----------------------------------------------------------

    def _resolve_symbol_token(self, symbol: str, exchange: str) -> str:
        """
        Resolve ``symbol``/``exchange`` to Angel One's ``symboltoken``,
        fetching and caching the full scrip master the first time it's
        needed. Cash-equity entries only (``instrumenttype == ""``) -
        excludes F&O contracts that might otherwise share the same
        ``name``.
        """
        if not self._symbol_token_cache:
            self._load_scrip_master()

        token = self._symbol_token_cache.get((exchange, symbol))
        if token is None:
            raise UnsupportedSymbolError(
                f"Angel One scrip master has no cash-equity entry for '{exchange}:{symbol}'"
            )
        return token

    def _load_scrip_master(self) -> None:
        def fetch() -> list[dict[str, Any]]:
            request = urllib.request.Request(
                _SCRIP_MASTER_URL, headers={"User-Agent": "AQTPAngelOneFeed/1.0"}
            )
            with urllib.request.urlopen(request, timeout=_SCRIP_MASTER_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            rows = retry_with_backoff(fetch, policy=_RETRY_POLICY, is_retryable=lambda exc: True)
        except Exception as exc:  # noqa: BLE001
            raise MarketDataTransportError(f"Angel One scrip master fetch failed: {exc}") from exc

        for row in rows:
            if row.get("instrumenttype") == "" and row.get("exch_seg") and row.get("name"):
                self._symbol_token_cache[(row["exch_seg"], row["name"])] = row["token"]

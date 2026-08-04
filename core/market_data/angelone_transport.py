"""
Angel One (SmartAPI) market-data transport - historical candles plus a
poll-based LTP live feed.

Angel One is never a broker (order placement) in this platform - Kite/
Groww remain the only ones, and this module is never wired into
``core/broker/``. It can now serve as a ``market_data_provider`` for a
*paper* session, though (see ``core/market_data/angelone_live_feed.py``
and ``core/market_data/factory.py``'s ``"angelone"`` branch) - Angel One
has no separate paper/sandbox market-data mode, same as Groww/Kite, so
"live" here just means real quotes, not permission to place real orders.

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
  "instrumenttype": "", "exch_seg": "NSE", ...}``). ``ltpData`` also
  needs the exact ``tradingsymbol`` string (``"RELIANCE-EQ"``, the
  ``symbol`` field) alongside the token, so the scrip cache keeps both.
- ``ltpData(exchange, tradingsymbol, symboltoken)``'s response:
  ``{"status": true, "message": "SUCCESS", "errorcode": "", "data":
  {"exchange", "tradingsymbol", "symboltoken", "open", "high", "low",
  "close", "ltp"}}`` (verified against the SmartAPI docs' own example
  response). Unlike Groww/Kite's quote payloads, there is no volume or
  trade-timestamp field at all - ``AngelOneLiveMarketDataFeed`` fills
  those in itself; see its docstring for what that means.
- ``getMarketData(mode, exchangeTokens)`` (the bulk ``/quote`` endpoint,
  used by ``get_ltp_batch`` for the candidate prescreen - see
  ``core/market_data/candidate_prescreen.py``) is a genuinely different
  wire format from ``ltpData`` despite both returning an LTP: request is
  ``{"mode": "LTP", "exchangeTokens": {"NSE": ["881", ...]}}`` (up to 50
  tokens per exchange per request - SmartAPI's documented per-request
  cap, verified against the SmartAPI forum's own worked example, along
  with the "1 request per second" rate limit), response is
  ``{"status": true, ..., "data": {"fetched": [{"exchange",
  "tradingSymbol", "symbolToken", "ltp"}, ...], "unfetched": [...]}}`` -
  note ``tradingSymbol``/``symbolToken`` are camelCase here, unlike
  ``ltpData``'s snake_case ``tradingsymbol``/``symboltoken`` - genuinely
  a different endpoint's convention, not a typo.
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
import logging
import re
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import logzero
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

# smartapi-python's SmartConnect.__init__ unconditionally does
# `logzero.logfile(f"logs/{today}/app.log", loglevel=logging.ERROR)` and,
# on ANY request exception (timeout, DNS failure, etc.), logs the full
# request headers verbatim - including the bearer access token and the
# X-PrivateKey API key - via `logger.error(f"...Headers: {headers}...")`
# in its own `_request()` (verified in the installed SDK's source, not
# guessed). This has already written a real token/key to
# logs/<date>/app.log in plaintext. Two independent mitigations, applied
# once at import time (before this module ever constructs a
# SmartConnect): stop the SDK from creating that file handler at all, and
# redact secret-shaped values from every record its shared `logzero.logger`
# emits (covers logzero's own default stderr handler too, which is not
# gated by `logfile()` and could otherwise leak into cron-redirected
# output). This is a mitigation for a third-party SDK's behavior, not a
# statement that this module itself ever logs secrets - it doesn't.
_SECRET_KEYS = ("Authorization", "X-PrivateKey", "access_token", "api_key", "feed_token", "refresh_token")
_SECRET_VALUE_PATTERN = re.compile(
    r"(['\"](?:" + "|".join(_SECRET_KEYS) + r")['\"]\s*:\s*['\"])[^'\"]*(['\"])",
    re.IGNORECASE,
)


class _SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _SECRET_VALUE_PATTERN.sub(r"\1***REDACTED***\2", record.msg)
        return True


def _harden_smartapi_logging() -> None:
    logzero.logfile = lambda *args, **kwargs: None  # SmartConnect() calls this itself, every time
    logzero.logger.addFilter(_SecretRedactingFilter())


_harden_smartapi_logging()

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

# SmartAPI's documented per-request cap for the bulk /quote endpoint
# (getMarketData) and its rate limit - see get_ltp_batch.
LTP_BATCH_SIZE = 50
LTP_BATCH_PACING_SECONDS = 1.0


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
        # (exchange, name) -> (symboltoken, tradingsymbol)
        self._scrip_cache: dict[tuple[str, str], tuple[str, str]] = {}
        # Same shape, but for index rows (instrumenttype == "AMXIDX", e.g. India VIX) -
        # kept separate from _scrip_cache so a cash-equity lookup can never
        # accidentally resolve an index, or vice versa. Both populate from the
        # same single scrip-master fetch - see _load_scrip_master.
        self._index_scrip_cache: dict[tuple[str, str], tuple[str, str]] = {}
        self._scrip_master_loaded = False

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
        self, symbol: str, exchange: str, interval: str, from_date: str, to_date: str, *, is_index: bool = False
    ) -> list[list[Any]]:
        """
        Fetch historical OHLCV candles for ``symbol`` on ``exchange``.

        ``interval`` must be one of ``MAX_DAYS_PER_INTERVAL``'s keys;
        ``from_date``/``to_date`` must be ``"yyyy-MM-dd HH:mm"`` (Angel
        One's required format, verified from the docs). Resolves
        ``symbol``/``exchange`` to a ``symboltoken`` via the scrip master
        first (required by ``getCandleData``).

        ``is_index=True`` resolves via the separate index scrip cache
        instead (e.g. ``symbol="INDIA VIX"``, ``exchange="NSE"``) - an
        index's scrip-master row has ``instrumenttype == "AMXIDX"``, not
        the cash-equity ``""``, so it's invisible to the default lookup.
        Default ``False`` preserves every existing caller's behavior
        exactly.
        """
        symbol_token = self._resolve_index_token(symbol, exchange) if is_index else self._resolve_symbol_token(
            symbol, exchange
        )
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

    def get_ltp(self, symbol: str, exchange: str) -> dict[str, Any]:
        """
        Fetch the last-traded-price snapshot for ``symbol`` on
        ``exchange`` (see module docstring for the verified response
        shape - no volume or trade-timestamp field).
        """
        symbol_token, trading_symbol = self._resolve_scrip(symbol, exchange)
        response = self._call(
            "get_ltp", lambda: self._client.ltpData(exchange, trading_symbol, symbol_token)
        )
        response = _raise_if_status_false(response, "get_ltp", is_auth=False)
        return response.get("data") or {}

    def get_ltp_batch(
        self,
        symbols: list[tuple[str, str]],
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> dict[tuple[str, str], float]:
        """
        Fetch LTP for many ``(symbol, exchange)`` pairs via the bulk
        ``/quote`` endpoint (see module docstring for the verified
        request/response shape), batching internally at
        ``LTP_BATCH_SIZE`` per request and pacing ``LTP_BATCH_PACING_SECONDS``
        between requests (SmartAPI's documented rate limit for this
        endpoint) - callers never need to know about either limit.

        Best-effort, not fail-fast: a symbol this transport can't resolve
        to a token, or one SmartAPI reports in ``unfetched``, is simply
        absent from the returned dict rather than raising - one bad
        symbol in a large candidate list shouldn't lose every other
        symbol's price. Only a genuine request-level failure (auth,
        network, a non-2xx/``status: false`` response) raises, via the
        same ``_call``/``_raise_if_status_false`` path every other method
        here uses.
        """
        token_to_key: dict[str, tuple[str, str]] = {}
        tokens_by_exchange: dict[str, list[str]] = {}
        for symbol, exchange in symbols:
            try:
                token, _trading_symbol = self._resolve_scrip(symbol, exchange)
            except UnsupportedSymbolError:
                continue
            token_to_key[token] = (symbol, exchange)
            tokens_by_exchange.setdefault(exchange, []).append(token)

        result: dict[tuple[str, str], float] = {}
        is_first_batch = True
        for exchange, tokens in tokens_by_exchange.items():
            for start in range(0, len(tokens), LTP_BATCH_SIZE):
                if not is_first_batch:
                    sleep_fn(LTP_BATCH_PACING_SECONDS)
                is_first_batch = False

                batch = tokens[start : start + LTP_BATCH_SIZE]
                response = self._call(
                    "get_ltp_batch",
                    lambda batch=batch, exchange=exchange: self._client.getMarketData(
                        "LTP", {exchange: batch}
                    ),
                )
                response = _raise_if_status_false(response, "get_ltp_batch", is_auth=False)
                data = response.get("data") or {}
                for item in data.get("fetched") or []:
                    key = token_to_key.get(str(item.get("symbolToken")))
                    if key is not None and "ltp" in item:
                        result[key] = float(item["ltp"])

        return result

    # -- internals -----------------------------------------------------------

    def _resolve_symbol_token(self, symbol: str, exchange: str) -> str:
        """Resolve ``symbol``/``exchange`` to Angel One's ``symboltoken`` alone."""
        return self._resolve_scrip(symbol, exchange)[0]

    def _resolve_scrip(self, symbol: str, exchange: str) -> tuple[str, str]:
        """
        Resolve ``symbol``/``exchange`` to Angel One's ``(symboltoken,
        tradingsymbol)``, fetching and caching the full scrip master the
        first time it's needed. Cash-equity entries only
        (``instrumenttype == ""``) - excludes F&O contracts that might
        otherwise share the same ``name``.
        """
        if not self._scrip_master_loaded:
            self._load_scrip_master()

        scrip = self._scrip_cache.get((exchange, symbol))
        if scrip is None:
            raise UnsupportedSymbolError(
                f"Angel One scrip master has no cash-equity entry for '{exchange}:{symbol}'"
            )
        return scrip

    def _resolve_index_token(self, symbol: str, exchange: str) -> str:
        """Resolve ``symbol``/``exchange`` (e.g. ``"INDIA VIX"``/``"NSE"``) to an index's ``symboltoken``."""
        if not self._scrip_master_loaded:
            self._load_scrip_master()

        scrip = self._index_scrip_cache.get((exchange, symbol))
        if scrip is None:
            raise UnsupportedSymbolError(
                f"Angel One scrip master has no index entry for '{exchange}:{symbol}'"
            )
        return scrip[0]

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
            if not row.get("exch_seg") or not row.get("name"):
                continue
            if row.get("instrumenttype") == "":
                self._scrip_cache[(row["exch_seg"], row["name"])] = (row["token"], row["symbol"])
            elif row.get("instrumenttype") == "AMXIDX":
                self._index_scrip_cache[(row["exch_seg"], row["name"])] = (row["token"], row["symbol"])
        self._scrip_master_loaded = True

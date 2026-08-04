"""
Kite (Zerodha Kite Connect) market-data transport V1 - wraps the official
``kiteconnect`` SDK.

Verified against the official ``kiteconnect`` SDK source
(https://github.com/zerodha/pykiteconnect) and Kite Connect's published
API docs - not guessed. Specifically:

- Auth: identical to the broker side (see
  ``core/broker/kite_transport.py``) - an already-issued ``access_token``
  (obtained out of band via ``scripts/kite_generate_access_token.py``),
  validated with one ``profile()`` call at construction.
- ``quote()`` keys its response by ``"EXCHANGE:TRADINGSYMBOL"`` and
  returns, per symbol, ``{"last_price", "ohlc": {...}, "volume",
  "last_trade_time", ...}`` - ``last_trade_time``/``timestamp`` are
  already parsed into ``datetime.datetime`` by the SDK itself (unlike
  Groww, which hands back raw epoch milliseconds).
- ``historical_data()`` requires a numeric ``instrument_token``, not a
  plain symbol+exchange pair (unlike Groww's ``get_historical_candles``) -
  resolved here via ``instruments(exchange)``, Kite's full instrument
  master dump, cached per-transport-instance per exchange since it's a
  genuinely large, slow call not worth repeating per symbol.
- ``historical_data()`` already returns parsed
  ``[{"date", "open", "high", "low", "close", "volume"}, ...]`` records,
  not Groww's raw positional candle arrays.

Deliberately independent of ``core/broker/kite_transport.py``: market
data and order execution are different concerns, matching
``groww_transport.py``'s (both the broker and market-data versions)
identical reasoning - the two modules share a design pattern, not code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kiteconnect import KiteConnect
from kiteconnect.exceptions import PermissionException, TokenException

from core.market_data.errors import (
    MarketDataAuthenticationError,
    MarketDataProviderError,
    MarketDataTransportError,
    UnsupportedSymbolError,
)
from core.utils.resilience import CircuitBreaker, CircuitOpenError, RetryPolicy, retry_with_backoff

_RETRY_POLICY = RetryPolicy()


def _is_retryable(exc: Exception) -> bool:
    """
    Retry a pure transport failure (network/timeout) or a 5xx/429 API
    response - never an auth failure, never an ordinary 4xx. Mirrors
    ``core/broker/kite_transport.py``'s identical reasoning.
    """
    if isinstance(exc, MarketDataAuthenticationError):
        return False
    if isinstance(exc, MarketDataProviderError):
        return exc.status_code is None or exc.status_code >= 500 or exc.status_code == 429
    return isinstance(exc, MarketDataTransportError)


def _translate_exception(exc: Exception, action: str) -> MarketDataTransportError:
    """Map a real ``kiteconnect`` exception onto this platform's shared ``MarketDataError`` hierarchy."""
    if isinstance(exc, (TokenException, PermissionException)):
        return MarketDataAuthenticationError(
            f"Kite market data authentication failed during {action}: {exc}", status_code=exc.code
        )
    code = getattr(exc, "code", None)
    if code is not None:
        return MarketDataProviderError(
            f"Kite market data API returned an error during {action}: {exc}", status_code=code
        )
    return MarketDataTransportError(f"Kite market data request failed during {action}: {exc}")


@dataclass(frozen=True)
class KiteMarketDataCredentials:
    """API credentials for authenticating against Kite's market-data API - see ``KiteCredentials``."""

    api_key: str
    access_token: str


@dataclass(frozen=True)
class KiteMarketDataTransportConfig:
    """Everything ``KiteMarketDataTransport`` needs: credentials and how long to wait per call."""

    credentials: KiteMarketDataCredentials
    timeout_seconds: float = 10.0


class KiteMarketDataTransport:
    """
    Thin wrapper around ``kiteconnect.KiteConnect`` for market-data calls.

    Every public method sends one request and returns plain
    ``dict``/``list`` data, matching ``groww_transport.py``'s (market-data
    version) contract exactly - ``KiteLiveMarketDataFeed``/
    ``KiteHistoricalMarketDataFeed`` are unaffected by this class's
    internals.

    Both ``get_quote``/``get_historical_candles`` are pure reads, so both
    retry with backoff on a transient failure, and both share one
    ``CircuitBreaker`` across the transport instance.
    """

    def __init__(
        self, config: KiteMarketDataTransportConfig, client: KiteConnect | None = None
    ) -> None:
        self._config = config
        self._circuit_breaker = CircuitBreaker()
        self._client = client if client is not None else self._authenticate(config)
        self._instrument_token_cache: dict[tuple[str, str], int] = {}

    def _authenticate(self, config: KiteMarketDataTransportConfig) -> KiteConnect:
        def do_auth() -> KiteConnect:
            client = KiteConnect(
                api_key=config.credentials.api_key,
                access_token=config.credentials.access_token,
                timeout=config.timeout_seconds,
            )
            try:
                client.profile()
            except Exception as exc:  # noqa: BLE001 - translated into our own hierarchy below
                raise _translate_exception(exc, "authentication") from exc
            return client

        return retry_with_backoff(do_auth, policy=_RETRY_POLICY, is_retryable=_is_retryable)

    def _call(self, action: str, fn: Callable[[], Any]) -> Any:
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
            raise MarketDataTransportError(f"Kite market data circuit open, refusing {action}: {exc}") from exc

    # -- public operations -------------------------------------------

    def get_quote(self, symbol: str, exchange: str) -> dict[str, Any]:
        """Fetch the latest quote for ``symbol`` on ``exchange``."""
        key = f"{exchange}:{symbol}"
        data = self._call("get_quote", lambda: self._client.quote(key))
        if key not in data:
            raise UnsupportedSymbolError(f"Kite market data does not recognize symbol '{key}'")
        return data[key]

    def get_quotes(self, symbols_with_exchange: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
        """
        Batch-fetch quotes for multiple ``(symbol, exchange)`` pairs in
        one call - Kite's ``quote()`` accepts several
        ``exchange:tradingsymbol`` keys at once, so scanning an option
        chain's contracts is one API call instead of one per contract.
        Keyed by ``"EXCHANGE:TRADINGSYMBOL"``; per Kite's docs, a key
        with no data (e.g. an expired/delisted contract) is simply
        absent from the response rather than an error.
        """
        keys = [f"{exchange}:{symbol}" for symbol, exchange in symbols_with_exchange]
        return self._call("get_quotes", lambda: self._client.quote(*keys))

    def get_historical_candles(
        self,
        symbol: str,
        exchange: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> list[dict[str, Any]]:
        """
        Fetch historical OHLCV candles for ``symbol`` on ``exchange``.

        Resolves ``symbol``/``exchange`` to Kite's numeric
        ``instrument_token`` first (required by ``historical_data``) -
        see ``_resolve_instrument_token``.
        """
        instrument_token = self._resolve_instrument_token(symbol, exchange)
        return self._call(
            "get_historical_candles",
            lambda: self._client.historical_data(instrument_token, from_date, to_date, interval),
        )

    def list_instruments(self, exchange: str) -> list[dict[str, Any]]:
        """
        Fetch Kite's full instrument master for ``exchange``: identifiers
        (``instrument_token``, ``tradingsymbol``, ``exchange``,
        ``segment``, ...) for every listed instrument - NOT fundamentals
        or volume data.
        """
        return self._call("list_instruments", lambda: self._client.instruments(exchange))

    # -- internals -----------------------------------------------------------

    def _resolve_instrument_token(self, symbol: str, exchange: str) -> int:
        """
        Resolve ``symbol``/``exchange`` to Kite's numeric
        ``instrument_token``, fetching and caching the full instrument
        dump for ``exchange`` the first time it's needed - a genuinely
        large, slow call not worth repeating per symbol or per historical
        fetch.
        """
        cache_key = (exchange, symbol)
        if cache_key not in self._instrument_token_cache:
            for row in self.list_instruments(exchange):
                self._instrument_token_cache[(exchange, row["tradingsymbol"])] = row["instrument_token"]

        token = self._instrument_token_cache.get(cache_key)
        if token is None:
            raise UnsupportedSymbolError(
                f"Kite instrument master for exchange '{exchange}' has no entry for symbol '{symbol}'"
            )
        return token

"""
Groww market-data transport V1 - wraps the official ``growwapi`` SDK.

Verified against Groww's official documentation and the installed
``growwapi`` package itself (v1.5.0) - not guessed. Specifically:

- Auth: identical exchange as the broker side (see
  ``core/broker/groww_transport.py``) - ``GrowwAPI.get_access_token(api_key,
  secret=secret)`` then ``GrowwAPI(token)``.
- ``get_quote`` response shape: https://groww.in/trade-api/docs/python-sdk/live-data
- ``get_historical_candles`` request shape and per-interval max lookback
  window: https://groww.in/trade-api/docs/python-sdk/historical-data
  (the exact V2 response shape used by ``get_historical_candles`` itself
  was not shown with a literal example on that page - only its
  predecessor, ``get_historical_candle_data``, was. Both are documented
  as returning the same conceptual candle data; ``groww_historical_feed.py``
  assumes the same array-based candle shape and flags this as the one
  remaining not-fully-verified piece.)

Deliberately independent of ``core/broker/groww_transport.py``: market
data and order execution are different concerns, and this keeps
``core/market_data/`` fully self-contained with no dependency on
``core/broker/``. The two modules share a design pattern (config
dataclass + isolated SDK-wrapping seam + explicit error hierarchy)
rather than code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from growwapi import GrowwAPI
from growwapi.groww.exceptions import (
    GrowwAPIAuthenticationException,
    GrowwAPIAuthorisationException,
    GrowwAPIException,
)

from core.market_data.errors import (
    MarketDataAuthenticationError,
    MarketDataProviderError,
    MarketDataTransportError,
)
from core.utils.resilience import CircuitBreaker, CircuitOpenError, RetryPolicy, retry_with_backoff
from core.utils.totp import generate_totp

# This platform only trades NSE/BSE cash equities in V1 (see CLAUDE.md) -
# every call is scoped to the CASH segment.
_SEGMENT = GrowwAPI.SEGMENT_CASH

_RETRY_POLICY = RetryPolicy()

_T = TypeVar("_T")


def _is_retryable(exc: Exception) -> bool:
    """
    Retry a pure transport failure (network/timeout) or a 5xx/429 API
    response - never an auth failure (retrying bad credentials just
    wastes time) and never an ordinary 4xx (the request itself is
    wrong, retrying it won't change that).
    """
    if isinstance(exc, MarketDataAuthenticationError):
        return False
    if isinstance(exc, MarketDataProviderError):
        return exc.status_code is None or exc.status_code >= 500 or exc.status_code == 429
    return isinstance(exc, MarketDataTransportError)


@dataclass(frozen=True)
class GrowwMarketDataCredentials:
    """
    API credentials for authenticating against Groww's market-data API.

    ``api_key`` + ``secret`` are the two values Groww's "Generate TOTP
    token" dashboard flow hands out together - ``secret`` is a base32
    TOTP seed, not a static password; see
    ``core/broker/groww_transport.py``'s ``GrowwCredentials`` docstring
    for the full explanation (same pattern on the order-execution side).
    """

    api_key: str
    secret: str


@dataclass(frozen=True)
class GrowwMarketDataTransportConfig:
    """Everything ``GrowwMarketDataTransport`` needs: credentials and how long to wait per call."""

    credentials: GrowwMarketDataCredentials
    timeout_seconds: float = 10.0


def _translate_exception(exc: Exception, action: str) -> MarketDataTransportError:
    """Map a real ``growwapi`` exception onto this platform's own ``MarketDataError`` hierarchy."""
    if isinstance(exc, (GrowwAPIAuthenticationException, GrowwAPIAuthorisationException)):
        return MarketDataAuthenticationError(
            f"Groww market data authentication failed during {action}: {exc}"
        )
    if isinstance(exc, GrowwAPIException):
        return MarketDataProviderError(
            f"Groww market data API returned an error during {action}: {exc}",
            status_code=int(exc.code) if str(exc.code).isdigit() else None,
        )
    return MarketDataTransportError(f"Groww market data request failed during {action}: {exc}")


class GrowwMarketDataTransport:
    """
    Thin wrapper around ``growwapi.GrowwAPI`` for market-data calls.

    Every public method sends one request and returns the decoded JSON
    body as a ``dict``, matching the previous raw-HTTP transport's
    contract exactly - ``GrowwLiveMarketDataFeed``/
    ``GrowwHistoricalMarketDataFeed`` are unaffected by this class's
    internals. Auth happens once, at construction, so a bad credential
    fails fast before any quote/candle call is attempted.

    Both calls are pure reads, so both retry with backoff on a transient
    failure, and both share one ``CircuitBreaker`` across the transport
    instance - repeated failures on either call trip the same breaker,
    since a down market-data provider affects both the same way (see
    ``core/broker/groww_transport.py``'s equivalent for the same pattern
    on the order-execution side, where writes additionally never retry).
    """

    def __init__(
        self, config: GrowwMarketDataTransportConfig, client: GrowwAPI | None = None
    ) -> None:
        self._config = config
        self._circuit_breaker = CircuitBreaker()
        self._client = client if client is not None else self._authenticate(config)

    def _authenticate(self, config: GrowwMarketDataTransportConfig) -> GrowwAPI:
        def do_auth() -> GrowwAPI:
            try:
                token = GrowwAPI.get_access_token(
                    api_key=config.credentials.api_key,
                    totp=generate_totp(config.credentials.secret),
                )
            except Exception as exc:  # noqa: BLE001 - translated into our own hierarchy below
                raise _translate_exception(exc, "authentication") from exc
            return GrowwAPI(token)

        return retry_with_backoff(do_auth, policy=_RETRY_POLICY, is_retryable=_is_retryable)

    def _call(self, action: str, fn: Callable[[], _T]) -> _T:
        def guarded() -> _T:
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
                f"Groww market data circuit open, refusing {action}: {exc}"
            ) from exc

    # -- public operations -------------------------------------------

    def get_quote(self, symbol: str, exchange: str) -> dict[str, Any]:
        """Fetch the latest quote for ``symbol`` on ``exchange``."""
        return self._call(
            "get_quote",
            lambda: self._client.get_quote(
                exchange=exchange,
                segment=_SEGMENT,
                trading_symbol=symbol,
                timeout=self._config.timeout_seconds,
            ),
        )

    def get_historical_candles(
        self,
        symbol: str,
        exchange: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> dict[str, Any]:
        """
        Fetch historical OHLCV candles for ``symbol`` on ``exchange``.

        ``from_date``/``to_date`` must already be in Groww's required
        ``"yyyy-MM-dd HH:mm:ss"`` format (see
        ``GrowwHistoricalMarketDataFeed``, which builds them) -
        ``groww_symbol`` (Groww's combined ``"EXCHANGE-SYMBOL"`` format,
        e.g. ``"NSE-RELIANCE"``) is built here, since joining
        exchange+symbol into that wire format is a Groww-specific detail
        this transport owns, not the feed.
        """
        groww_symbol = f"{exchange}-{symbol}"
        return self._call(
            "get_historical_candles",
            lambda: self._client.get_historical_candles(
                exchange=exchange,
                segment=_SEGMENT,
                groww_symbol=groww_symbol,
                start_time=from_date,
                end_time=to_date,
                candle_interval=interval,
                timeout=self._config.timeout_seconds,
            ),
        )

    def list_all_instruments(self) -> list[dict[str, Any]]:
        """
        Fetch Groww's full instrument master: identifiers (trading_symbol,
        exchange, segment, series, isin, ...) and lot size/tick size for
        every listed instrument across all exchanges/segments - NOT
        fundamentals, market cap, or volume data (see
        ``core/market_data/universe_screener.py`` for what screens on top
        of this).

        The SDK's ``get_all_instruments()`` returns a pandas ``DataFrame``;
        converted to plain dicts here (one per row) so the rest of the
        codebase never needs to depend on pandas directly, matching every
        other transport method here returning plain dict/list data.
        """
        return self._call(
            "get_all_instruments", lambda: self._client.get_all_instruments().to_dict("records")
        )

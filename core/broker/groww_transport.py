"""
Groww transport V1 - wraps the official ``growwapi`` SDK.

Verified against Groww's official documentation and the installed
``growwapi`` package itself (v1.5.0) - not guessed. Specifically:

- Auth: ``GrowwAPI.get_access_token(api_key, secret=secret)`` exchanges
  an API key + secret (the credentials Groww's dashboard actually
  generates - see ``groww_models.py``) for a short-lived access token;
  ``GrowwAPI(token)`` is then the authenticated client every other call
  goes through. Source: https://groww.in/trade-api/docs/python-sdk
- Order/position field names and the full ``order_status`` vocabulary:
  https://groww.in/trade-api/docs/python-sdk/orders and
  https://groww.in/trade-api/docs/python-sdk/annexures
- Positions response shape: https://groww.in/trade-api/docs/python-sdk/portfolio

This is the only place in ``core/broker/`` that imports ``growwapi``
directly - ``GrowwBrokerAdapter`` and ``groww_models.py`` only ever see
plain ``dict`` JSON payloads (mirroring the previous raw-HTTP transport's
contract), so swapping the underlying SDK again later would not require
touching the adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from growwapi import GrowwAPI
from growwapi.groww.exceptions import (
    GrowwAPIAuthenticationException,
    GrowwAPIAuthorisationException,
    GrowwAPIException,
)

from core.broker.groww_errors import GrowwApiError, GrowwAuthenticationError, GrowwTransportError
from core.broker.groww_models import GrowwCredentials
from core.utils.resilience import CircuitBreaker, CircuitOpenError, RetryPolicy, retry_with_backoff
from core.utils.totp import generate_totp

# Every call defaults to the CASH segment (this platform's only kind
# until the options work) unless the caller passes ``segment=`` - every
# existing call site is unaffected. ``get_order``/``cancel_order`` are
# not segment-aware even so: they take only an order id (matching
# ``Broker.get_order``/``cancel_order``'s signature), and nothing in V1
# yet needs to cancel/fetch an NFO order by id (naked writes are held to
# square-off, never cancelled - see ``core/strategy/options_mispricing.py``).
_SEGMENT_CASH = GrowwAPI.SEGMENT_CASH
_SEGMENT_FNO = GrowwAPI.SEGMENT_FNO

_RETRY_POLICY = RetryPolicy()


def _is_retryable(exc: Exception) -> bool:
    """
    Retry a pure transport failure (network/timeout) or a 5xx/429 API
    response - never an auth failure (retrying bad credentials just
    wastes time) and never an ordinary 4xx (the request itself is
    wrong, retrying it won't change that).
    """
    if isinstance(exc, GrowwAuthenticationError):
        return False
    if isinstance(exc, GrowwApiError):
        return exc.status_code is None or exc.status_code >= 500 or exc.status_code == 429
    return isinstance(exc, GrowwTransportError)


@dataclass(frozen=True)
class GrowwTransportConfig:
    """Everything ``GrowwHttpTransport`` needs: credentials and how long to wait per call."""

    credentials: GrowwCredentials
    timeout_seconds: float = 10.0


def _translate_exception(exc: Exception, action: str) -> GrowwTransportError:
    """
    Map a real ``growwapi`` exception onto this platform's own
    ``GrowwError`` hierarchy, so nothing outside this module needs to
    know the SDK's exception types exist - matching how the previous
    raw-HTTP transport translated status codes.
    """
    if isinstance(exc, (GrowwAPIAuthenticationException, GrowwAPIAuthorisationException)):
        return GrowwAuthenticationError(f"Groww authentication failed during {action}: {exc}")
    if isinstance(exc, GrowwAPIException):
        return GrowwApiError(
            f"Groww API returned an error during {action}: {exc}",
            status_code=int(exc.code) if str(exc.code).isdigit() else None,
        )
    return GrowwTransportError(f"Groww request failed during {action}: {exc}")


class GrowwHttpTransport:
    """
    Thin wrapper around ``growwapi.GrowwAPI``.

    Every public method sends one request and returns the decoded JSON
    body as a ``dict``, so ``GrowwBrokerAdapter``/``groww_models.py`` are
    unaffected by this class's internals - matching the previous raw-HTTP
    transport's contract exactly. Auth (the api_key+secret -> access
    token exchange) happens once, at construction, so a bad credential
    fails fast before any order/position call is attempted.

    Every call goes through one ``CircuitBreaker`` shared across the
    whole transport instance: if Groww is down, it's down for every
    endpoint, so repeated failures on any call trip the same breaker and
    every subsequent call (read or write) fails fast instead of piling
    up timeouts. Reads (``get_order``/``list_positions``) additionally
    retry with backoff - safe, since re-fetching state can't duplicate
    anything. Writes (``place_order``/``cancel_order``) never
    auto-retry: Groww's idempotent-submission guarantees for a retried
    write aren't verified, and a spurious duplicate order is a strictly
    worse failure mode than a fast, visible error the caller must decide
    how to handle.
    """

    def __init__(self, config: GrowwTransportConfig, client: GrowwAPI | None = None) -> None:
        self._config = config
        self._circuit_breaker = CircuitBreaker()
        self._client = client if client is not None else self._authenticate(config)

    def _authenticate(self, config: GrowwTransportConfig) -> GrowwAPI:
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

    def _call(self, action: str, fn: Callable[[], dict[str, Any]], *, retryable: bool) -> dict[str, Any]:
        def guarded() -> dict[str, Any]:
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                raise _translate_exception(exc, action) from exc

        attempt: Callable[[], dict[str, Any]] = (
            (lambda: retry_with_backoff(guarded, policy=_RETRY_POLICY, is_retryable=_is_retryable))
            if retryable
            else guarded
        )
        try:
            return self._circuit_breaker.call(attempt)
        except CircuitOpenError as exc:
            raise GrowwTransportError(f"Groww circuit open, refusing {action}: {exc}") from exc

    # -- public operations ---------------------------------------------------

    def place_order(self, payload: dict[str, Any], segment: str = _SEGMENT_CASH) -> dict[str, Any]:
        """Place a new order. ``payload`` keys match ``GrowwAPI.place_order``'s kwargs."""
        return self._call(
            "place_order",
            lambda: self._client.place_order(
                segment=segment, timeout=self._config.timeout_seconds, **payload
            ),
            retryable=False,
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an existing order by id (CASH segment only - see module docstring)."""
        return self._call(
            "cancel_order",
            lambda: self._client.cancel_order(
                groww_order_id=order_id, segment=_SEGMENT_CASH, timeout=self._config.timeout_seconds
            ),
            retryable=False,
        )

    def get_order(self, order_id: str) -> dict[str, Any]:
        """
        Fetch full current state of an order by id (CASH segment only -
        see module docstring).

        Deliberately calls ``get_order_detail`` rather than
        ``get_order_status``: the latter's verified response only
        carries id/status/remark/filled_quantity, not enough to populate
        this platform's ``Order`` model (symbol/exchange/side/type/
        product/quantity are all required fields there) - only
        ``get_order_detail`` returns the full order shape.
        """
        return self._call(
            "get_order",
            lambda: self._client.get_order_detail(
                groww_order_id=order_id, segment=_SEGMENT_CASH, timeout=self._config.timeout_seconds
            ),
            retryable=True,
        )

    def list_positions(self, segment: str = _SEGMENT_CASH) -> dict[str, Any]:
        """Fetch all current positions for ``segment``. Verified shape: ``{"positions": [...]}``."""
        return self._call(
            "list_positions",
            lambda: self._client.get_positions_for_user(
                segment=segment, timeout=self._config.timeout_seconds
            ),
            retryable=True,
        )

    def get_order_margin_details(self, orders: list[dict[str, Any]], segment: str = _SEGMENT_FNO) -> dict[str, Any]:
        """
        Batch order-margin calculator. Each ``orders`` item needs
        ``trading_symbol``/``transaction_type``/``quantity``/``price``/
        ``order_type``/``product``/``exchange`` (verified against the
        installed SDK's own request-building source). Response is one
        aggregate margin breakdown for the whole batch, keyed
        (verified: https://groww.in/trade-api/docs/python-sdk/margin)
        by ``total_requirement`` among other fields - not per-order.
        """
        return self._call(
            "get_order_margin_details",
            lambda: self._client.get_order_margin_details(
                segment=segment, orders=orders, timeout=self._config.timeout_seconds
            ),
            retryable=True,
        )

    def get_available_margin_details(self) -> dict[str, Any]:
        """
        Fetch the user's available margin/balance details. Verified
        shape (same docs page) includes ``clear_cash`` - the primary
        available-funds figure.
        """
        return self._call(
            "get_available_margin_details",
            lambda: self._client.get_available_margin_details(timeout=self._config.timeout_seconds),
            retryable=True,
        )

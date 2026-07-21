"""
Kite (Zerodha Kite Connect) transport V1 - wraps the official
``kiteconnect`` SDK.

Verified against the official ``kiteconnect`` SDK source
(https://github.com/zerodha/pykiteconnect) and Kite Connect's published
API docs - not guessed. Specifically:

- Auth: unlike Groww's TOTP-based exchange (a single headless call), Kite
  Connect requires a browser-based login redirect to obtain a short-lived
  ``request_token`` first (see ``scripts/kite_generate_access_token.py``,
  a separate manually-run helper), which is then exchanged for an
  ``access_token`` that is valid until 6 AM IST the next day. By the time
  this transport is constructed, that exchange has already happened out
  of band - ``KiteTransportConfig`` only ever carries the already-issued
  ``access_token``, never a password or TOTP seed. Construction still
  fails fast on a bad/expired token via one ``profile()`` call, mirroring
  Groww's fail-fast-on-construction behavior without re-doing the login
  exchange itself.
- ``place_order()``/``cancel_order()`` return a bare ``order_id`` string,
  not a status - see ``kite_models.py``'s module docstring for how the
  adapter handles this.
- ``order_history()`` returns the full chronological list of state
  transitions for one order; this transport returns the last (current)
  entry.
- ``positions()`` returns ``{"net": [...], "day": [...]}``; this platform
  uses ``"net"`` (current net holdings), matching "current position" the
  same way Groww's single ``positions`` list already does.

This is the only place in ``core/broker/`` that imports ``kiteconnect``
directly - ``KiteBrokerAdapter``/``kite_models.py`` only ever see plain
``dict`` JSON payloads, matching ``groww_transport.py``'s contract
exactly so the two providers are interchangeable from the adapter's
perspective.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kiteconnect import KiteConnect
from kiteconnect.exceptions import PermissionException, TokenException

from core.broker.kite_errors import KiteApiError, KiteAuthenticationError, KiteTransportError
from core.broker.kite_models import KiteCredentials
from core.utils.resilience import CircuitBreaker, CircuitOpenError, RetryPolicy, retry_with_backoff

_VARIETY_REGULAR = "regular"

_RETRY_POLICY = RetryPolicy()


def _is_retryable(exc: Exception) -> bool:
    """
    Retry a pure transport failure (network/timeout) or a 5xx/429 API
    response - never an auth failure (retrying bad/expired credentials
    just wastes time) and never an ordinary 4xx (the request itself is
    wrong, retrying it won't change that).
    """
    if isinstance(exc, KiteAuthenticationError):
        return False
    if isinstance(exc, KiteApiError):
        return exc.status_code is None or exc.status_code >= 500 or exc.status_code == 429
    return isinstance(exc, KiteTransportError)


def _translate_exception(exc: Exception, action: str) -> KiteTransportError:
    """
    Map a real ``kiteconnect`` exception onto this platform's own
    ``KiteError`` hierarchy, so nothing outside this module needs to know
    the SDK's exception types exist - matching how ``groww_transport.py``
    translates ``growwapi`` exceptions.

    ``TokenException``/``PermissionException`` are both 403-family
    auth/authorization failures (an expired ``access_token`` is by far
    the most common real-world cause - Kite invalidates it every day at
    6 AM IST) - both map to ``KiteAuthenticationError``, mirroring how
    Groww's two auth-flavored exceptions collapse into one.
    """
    if isinstance(exc, (TokenException, PermissionException)):
        return KiteAuthenticationError(f"Kite authentication failed during {action}: {exc}", status_code=exc.code)
    code = getattr(exc, "code", None)
    if code is not None:
        return KiteApiError(f"Kite API returned an error during {action}: {exc}", status_code=code)
    return KiteTransportError(f"Kite request failed during {action}: {exc}")


@dataclass(frozen=True)
class KiteTransportConfig:
    """Everything ``KiteHttpTransport`` needs: credentials and how long to wait per call."""

    credentials: KiteCredentials
    timeout_seconds: float = 10.0


class KiteHttpTransport:
    """
    Thin wrapper around ``kiteconnect.KiteConnect`` for order/position
    calls.

    Every public method sends one request and returns the decoded
    response as a ``dict``, so ``KiteBrokerAdapter``/``kite_models.py``
    are unaffected by this class's internals - matching
    ``groww_transport.py``'s contract. Construction validates
    ``access_token`` with one ``profile()`` call so a stale/expired token
    fails fast before any order/position call is attempted, rather than
    surfacing confusingly mid-session.

    Every call goes through one ``CircuitBreaker`` shared across the
    whole transport instance - matching ``GrowwHttpTransport``'s
    reasoning exactly. Reads (``get_order``/``list_positions``)
    additionally retry with backoff; writes (``place_order``/
    ``cancel_order``) never auto-retry, since a spurious duplicate order
    is a strictly worse failure mode than a fast, visible error.
    """

    def __init__(self, config: KiteTransportConfig, client: KiteConnect | None = None) -> None:
        self._config = config
        self._circuit_breaker = CircuitBreaker()
        self._client = client if client is not None else self._authenticate(config)

    def _authenticate(self, config: KiteTransportConfig) -> KiteConnect:
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

    def _call(self, action: str, fn: Callable[[], Any], *, retryable: bool) -> Any:
        def guarded() -> Any:
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                raise _translate_exception(exc, action) from exc

        attempt: Callable[[], Any] = (
            (lambda: retry_with_backoff(guarded, policy=_RETRY_POLICY, is_retryable=_is_retryable))
            if retryable
            else guarded
        )
        try:
            return self._circuit_breaker.call(attempt)
        except CircuitOpenError as exc:
            raise KiteTransportError(f"Kite circuit open, refusing {action}: {exc}") from exc

    # -- public operations ---------------------------------------------------

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Place a new order. ``payload`` keys match ``KiteConnect.place_order``'s
        kwargs (minus ``variety``, passed separately - the SDK takes it as
        a positional/keyword arg alongside the rest, not nested in the body).

        Returns ``{"order_id": <str>}`` - Kite's real placement response
        carries nothing else (see module docstring).
        """
        order_id = self._call(
            "place_order",
            lambda: self._client.place_order(variety=_VARIETY_REGULAR, **payload),
            retryable=False,
        )
        return {"order_id": order_id}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """
        Cancel an existing order by id.

        Returns ``{"order_id": <str>}`` on success. Kite raises (never
        returns a failure status) if the cancel itself fails - propagated
        as a ``KiteApiError``/``KiteTransportError`` by ``_call``.
        """
        returned_id = self._call(
            "cancel_order",
            lambda: self._client.cancel_order(variety=_VARIETY_REGULAR, order_id=order_id),
            retryable=False,
        )
        return {"order_id": returned_id}

    def get_order(self, order_id: str) -> dict[str, Any]:
        """
        Fetch the current full state of an order by id.

        ``order_history`` returns every state transition in chronological
        order; the last entry is the order's current state - the same
        full shape ``orders()`` returns per order.
        """
        history = self._call(
            "get_order", lambda: self._client.order_history(order_id=order_id), retryable=True
        )
        if not history:
            raise KiteTransportError(f"Kite returned no order history for order_id={order_id!r}")
        return history[-1]

    def list_positions(self) -> dict[str, Any]:
        """
        Fetch all current positions. Verified shape:
        ``{"net": [...], "day": [...]}``.
        """
        return self._call("list_positions", lambda: self._client.positions(), retryable=True)

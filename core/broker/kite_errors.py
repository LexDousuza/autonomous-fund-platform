"""
Exceptions for the Kite (Zerodha Kite Connect) broker integration
(transport + adapter).

Kept in one small module so both ``kite_transport.py`` and
``kite_adapter.py`` raise/catch a shared, explicit exception vocabulary
instead of letting raw ``kiteconnect`` SDK exceptions or generic
``ValueError``/``RuntimeError`` leak up through the ``Broker`` interface.
Mirrors ``core/broker/groww_errors.py``'s shape for consistency between
the two provider integrations.
"""

from __future__ import annotations


class KiteError(Exception):
    """Base class for all Kite integration errors."""


class KiteTransportError(KiteError):
    """
    A Kite API call could not be completed or its response could not be
    understood: connection failure, timeout, malformed payload, or a
    response missing fields the adapter requires.

    This is the base failure mode - "something went wrong trying to talk
    to Kite" - as opposed to ``KiteApiError``, which means we *did* get a
    response, and Kite's server told us the request itself failed.
    """


class KiteApiError(KiteTransportError):
    """
    Kite responded, but with an error (a ``kiteconnect.exceptions``
    subclass other than an auth failure). Carries the HTTP status code
    (when known) and raw error message for diagnostics.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class KiteAuthenticationError(KiteApiError):
    """
    Kite rejected the request as unauthenticated/unauthorized - almost
    always an expired or missing ``access_token`` (Kite access tokens
    expire at 6 AM IST every day, a regulatory requirement, not a bug).
    """

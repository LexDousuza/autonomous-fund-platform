"""
Exceptions for the Groww broker integration (transport + adapter).

Kept in one small module so both ``groww_transport.py`` and
``groww_adapter.py`` raise/catch a shared, explicit exception vocabulary
instead of letting raw ``urllib`` errors or generic ``ValueError``/
``RuntimeError`` leak up through the ``Broker`` interface.
"""

from __future__ import annotations


class GrowwError(Exception):
    """Base class for all Groww integration errors."""


class GrowwTransportError(GrowwError):
    """
    A Groww HTTP call could not be completed or its response could not be
    understood: connection failure, timeout, malformed JSON, or a
    response missing fields the adapter requires.

    This is the base failure mode - "something went wrong trying to talk
    to Groww" - as opposed to ``GrowwApiError``, which means we *did* get
    a response, and Groww's server told us the request itself failed.
    """


class GrowwApiError(GrowwTransportError):
    """
    Groww responded, but with a non-2xx status (other than an auth
    failure) or an explicit error payload. Carries the HTTP status code
    and raw response body for diagnostics.
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


class GrowwAuthenticationError(GrowwApiError):
    """Groww rejected the request as unauthenticated/unauthorized (401/403)."""

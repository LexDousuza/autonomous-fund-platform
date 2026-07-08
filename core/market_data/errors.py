"""
Exceptions for live market-data provider integrations.

Kept in one small module so provider transports/feeds (e.g.
``groww_transport.py``, ``groww_live_feed.py``) raise a shared, explicit
exception vocabulary instead of letting raw ``urllib`` errors or generic
``ValueError``/``RuntimeError`` leak up through the ``MarketDataFeed``
interface. Mirrors ``core/broker/groww_errors.py``'s shape for
consistency between the two provider integrations.
"""

from __future__ import annotations


class MarketDataError(Exception):
    """Base class for all live market-data integration errors."""


class MarketDataTransportError(MarketDataError):
    """
    A market-data provider call could not be completed or its response
    could not be understood: connection failure, timeout, malformed
    JSON, or a response missing fields the feed requires.
    """


class MarketDataProviderError(MarketDataTransportError):
    """
    The provider responded, but with a non-2xx status (other than an
    auth failure) or an explicit error payload. Carries the HTTP status
    code and raw response body for diagnostics.
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


class MarketDataAuthenticationError(MarketDataProviderError):
    """The provider rejected the request as unauthenticated/unauthorized (401/403)."""


class UnsupportedSymbolError(MarketDataError):
    """
    The provider's response explicitly indicates it does not recognize
    or support the requested symbol (as opposed to a generic API/
    transport failure).
    """

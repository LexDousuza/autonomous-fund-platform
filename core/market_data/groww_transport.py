"""
Groww market-data HTTP transport V1.

This is the only place in ``core/market_data/`` that knows about URLs,
auth headers, timeouts, raw request execution, and raw JSON decoding for
Groww's market-data endpoints. It has no knowledge of this platform's
domain objects (``MarketBar``) - it takes/returns plain ``dict`` JSON
payloads. ``GrowwLiveMarketDataFeed`` owns turning those payloads into
``MarketBar`` instances; this module must stay ignorant of that mapping.

Deliberately independent of ``core/broker/groww_transport.py``: market
data and order execution are different concerns (potentially different
auth scopes, definitely different endpoints/response shapes), and this
keeps ``core/market_data/`` fully self-contained with no dependency on
``core/broker/``. The two modules share a design pattern (config
dataclass + isolated raw-HTTP seam + explicit error hierarchy) rather
than code.

IMPORTANT ASSUMPTION: Groww's exact market-data endpoint path, auth
header scheme, and JSON quote payload shape are not verified against
real Groww API documentation as part of this task (no network access, no
real credentials). They are isolated below (as class-level constants and
in ``GrowwLiveMarketDataFeed``'s parsing) so they are easy to find and
correct once real API docs are available.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from core.market_data.errors import (
    MarketDataAuthenticationError,
    MarketDataProviderError,
    MarketDataTransportError,
)


@dataclass(frozen=True)
class GrowwMarketDataCredentials:
    """API credentials for authenticating against Groww's market-data API."""

    api_key: str
    access_token: str


@dataclass(frozen=True)
class GrowwMarketDataTransportConfig:
    """Everything ``GrowwMarketDataTransport`` needs: where to send requests, how to authenticate, how long to wait."""

    base_url: str
    credentials: GrowwMarketDataCredentials
    timeout_seconds: float = 10.0


class GrowwMarketDataTransport:
    """
    Low-level HTTP client for Groww's market-data endpoints.

    Every public method sends one request and returns the decoded JSON
    body as a ``dict``. Non-2xx responses, connection failures/timeouts,
    and malformed/non-object JSON bodies all raise a
    ``MarketDataTransportError`` subclass rather than returning ``None``
    or a partial result.
    """

    # Placeholder endpoint path - see module docstring.
    _GET_QUOTE_PATH = "/v1/market-data/quote"

    def __init__(self, config: GrowwMarketDataTransportConfig) -> None:
        self._config = config

    # -- public HTTP operations -------------------------------------------

    def get_quote(self, symbol: str, exchange: str) -> dict[str, Any]:
        """Fetch the latest quote/candle for ``symbol`` on ``exchange``."""
        path = f"{self._GET_QUOTE_PATH}?symbol={symbol}&exchange={exchange}"
        return self._request("GET", path)

    # -- request plumbing ---------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """
        Build auth headers for every request.

        Assumed scheme (see module docstring): bearer access token plus a
        separate API key header - the same assumed shape used by the
        broker-side transport, for consistency, though these are
        independent credentials/config objects.
        """
        return {
            "Authorization": f"Bearer {self._config.credentials.access_token}",
            "X-API-KEY": self._config.credentials.api_key,
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str) -> dict[str, Any]:
        url = f"{self._config.base_url.rstrip('/')}{path}"
        headers = self._auth_headers()

        status_code, raw_body = self._execute_request(method, url, headers)

        if not (200 <= status_code < 300):
            self._raise_for_status(method, path, status_code, raw_body)

        return self._parse_json(method, path, raw_body)

    def _execute_request(self, method: str, url: str, headers: dict[str, str]) -> tuple[int, str]:
        """
        Perform the raw HTTP call and return ``(status_code, raw_response_text)``.

        Isolated as its own method - the only place ``urllib`` is used -
        so tests can substitute a fake implementation without touching
        real sockets, and so every network-level failure mode is handled
        in exactly one place.
        """
        try:
            request = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise MarketDataTransportError(f"Groww market data request failed: {method} {url}: {exc}") from exc

    def _raise_for_status(self, method: str, path: str, status_code: int, raw_body: str) -> None:
        if status_code in (401, 403):
            raise MarketDataAuthenticationError(
                f"Groww market data authentication failed for {method} {path} (status={status_code})",
                status_code=status_code,
                response_body=raw_body,
            )
        raise MarketDataProviderError(
            f"Groww market data API returned an error for {method} {path} (status={status_code})",
            status_code=status_code,
            response_body=raw_body,
        )

    def _parse_json(self, method: str, path: str, raw_body: str) -> dict[str, Any]:
        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise MarketDataTransportError(
                f"Groww market data response for {method} {path} was not valid JSON: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise MarketDataTransportError(
                f"Groww market data response for {method} {path} was not a JSON object "
                f"(got {type(data).__name__})"
            )

        return data

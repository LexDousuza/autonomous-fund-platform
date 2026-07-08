"""
Groww HTTP transport V1.

This is the *only* place in the codebase that knows about URLs, auth
headers, timeouts, raw request execution, and raw JSON decoding for the
Groww API. It has no knowledge of this platform's domain objects
(``Order``, ``Position``, ...) - it takes/returns plain ``dict`` JSON
payloads. ``GrowwBrokerAdapter`` (and ``groww_models.py``'s ``from_*_json``
parsers) own turning those payloads into broker-domain models; this
module must stay ignorant of that mapping.

IMPORTANT ASSUMPTION: Groww's exact endpoint paths, auth header scheme,
and JSON payload shapes are not verified against real Groww API
documentation as part of this task (no network access, no real
credentials). The paths/headers below are a reasonable, clearly-isolated
placeholder shape (grouped as class-level constants) so they are easy to
find and correct once real API docs are available - do not treat them as
confirmed-correct without verification.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from core.broker.groww_errors import GrowwApiError, GrowwAuthenticationError, GrowwTransportError
from core.broker.groww_models import GrowwCredentials


@dataclass(frozen=True)
class GrowwTransportConfig:
    """Everything ``GrowwHttpTransport`` needs: where to send requests, how to authenticate, how long to wait."""

    base_url: str
    credentials: GrowwCredentials
    timeout_seconds: float = 10.0


class GrowwHttpTransport:
    """
    Low-level HTTP client for the Groww API.

    Every public method sends one request and returns the decoded JSON
    body as a ``dict``. Non-2xx responses, connection failures/timeouts,
    and malformed/non-object JSON bodies all raise a ``GrowwTransportError``
    subclass rather than returning ``None`` or a partial result.
    """

    # Placeholder endpoint paths - see module docstring.
    _PLACE_ORDER_PATH = "/v1/order/create"
    _CANCEL_ORDER_PATH = "/v1/order/{order_id}/cancel"
    _GET_ORDER_PATH = "/v1/order/{order_id}"
    _LIST_POSITIONS_PATH = "/v1/positions"

    def __init__(self, config: GrowwTransportConfig) -> None:
        self._config = config

    # -- public HTTP operations -------------------------------------------

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a new order payload; returns the decoded JSON response."""
        return self._request("POST", self._PLACE_ORDER_PATH, json_body=payload)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an existing order by id; returns the decoded JSON response."""
        return self._request("POST", self._CANCEL_ORDER_PATH.format(order_id=order_id))

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Fetch full current state of an order by id."""
        return self._request("GET", self._GET_ORDER_PATH.format(order_id=order_id))

    def list_positions(self) -> dict[str, Any]:
        """Fetch all current positions. Assumed shape: ``{"positions": [...]}``."""
        return self._request("GET", self._LIST_POSITIONS_PATH)

    # -- request plumbing ---------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """
        Build auth headers for every request.

        Assumed scheme (see module docstring): bearer access token plus a
        separate API key header. Adjust here, in one place, once the real
        Groww auth scheme is confirmed.
        """
        return {
            "Authorization": f"Bearer {self._config.credentials.access_token}",
            "X-API-KEY": self._config.credentials.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._config.base_url.rstrip('/')}{path}"
        headers = self._auth_headers()
        body = json.dumps(json_body).encode("utf-8") if json_body is not None else None

        status_code, raw_body = self._execute_request(method, url, headers, body)

        if not (200 <= status_code < 300):
            self._raise_for_status(method, path, status_code, raw_body)

        return self._parse_json(method, path, raw_body)

    def _execute_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> tuple[int, str]:
        """
        Perform the raw HTTP call and return ``(status_code, raw_response_text)``.

        Isolated as its own method - the only place ``urllib`` is used -
        so tests can substitute a fake implementation without touching
        real sockets, and so every network-level failure mode is handled
        in exactly one place.
        """
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise GrowwTransportError(f"Groww request failed: {method} {url}: {exc}") from exc

    def _raise_for_status(self, method: str, path: str, status_code: int, raw_body: str) -> None:
        if status_code in (401, 403):
            raise GrowwAuthenticationError(
                f"Groww authentication failed for {method} {path} (status={status_code})",
                status_code=status_code,
                response_body=raw_body,
            )
        raise GrowwApiError(
            f"Groww API returned an error for {method} {path} (status={status_code})",
            status_code=status_code,
            response_body=raw_body,
        )

    def _parse_json(self, method: str, path: str, raw_body: str) -> dict[str, Any]:
        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise GrowwTransportError(
                f"Groww response for {method} {path} was not valid JSON: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise GrowwTransportError(
                f"Groww response for {method} {path} was not a JSON object "
                f"(got {type(data).__name__})"
            )

        return data

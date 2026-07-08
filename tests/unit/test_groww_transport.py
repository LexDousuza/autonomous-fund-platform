"""
Unit tests for core.broker.groww_transport.

These tests never touch a real socket: ``GrowwHttpTransport._execute_request``
is the sole seam that performs raw HTTP I/O, so tests substitute a fake
implementation of it to simulate success, HTTP error, timeout/connection
failure, and malformed-response scenarios deterministically.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_groww_transport.py -v
"""
from __future__ import annotations

import json
import unittest

from core.broker.groww_errors import GrowwApiError, GrowwAuthenticationError, GrowwTransportError
from core.broker.groww_models import GrowwCredentials
from core.broker.groww_transport import GrowwHttpTransport, GrowwTransportConfig


def make_transport(base_url: str = "https://api.groww.example") -> GrowwHttpTransport:
    config = GrowwTransportConfig(
        base_url=base_url,
        credentials=GrowwCredentials(api_key="test-api-key", access_token="test-access-token"),
        timeout_seconds=5.0,
    )
    return GrowwHttpTransport(config)


class GrowwHttpTransportAuthHeaderTests(unittest.TestCase):
    def test_builds_auth_headers_correctly(self) -> None:
        transport = make_transport()

        headers = transport._auth_headers()

        self.assertEqual(headers["Authorization"], "Bearer test-access-token")
        self.assertEqual(headers["X-API-KEY"], "test-api-key")
        self.assertEqual(headers["Content-Type"], "application/json")


class GrowwHttpTransportSuccessPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = make_transport()
        self.captured_calls: list[tuple[str, str, dict, bytes | None]] = []

    def _install_fake_execute(self, status_code: int, body: str) -> None:
        def fake_execute(method, url, headers, body_bytes):
            self.captured_calls.append((method, url, headers, body_bytes))
            return status_code, body

        self.transport._execute_request = fake_execute  # type: ignore[method-assign]

    def test_place_order_sends_expected_post_request_and_parses_response(self) -> None:
        self._install_fake_execute(
            200, json.dumps({"order_id": "GRW-1", "status": "NEW", "filled_quantity": 0})
        )

        result = self.transport.place_order(
            {"symbol": "RELIANCE", "exchange": "NSE", "side": "BUY", "quantity": 5}
        )

        self.assertEqual(result, {"order_id": "GRW-1", "status": "NEW", "filled_quantity": 0})

        method, url, headers, body_bytes = self.captured_calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/v1/order/create"))
        self.assertEqual(headers["Authorization"], "Bearer test-access-token")
        sent_payload = json.loads(body_bytes.decode("utf-8"))
        self.assertEqual(sent_payload["symbol"], "RELIANCE")
        self.assertEqual(sent_payload["quantity"], 5)

    def test_cancel_order_sends_expected_request_and_parses_response(self) -> None:
        self._install_fake_execute(200, json.dumps({"order_id": "GRW-2", "status": "CANCELLED"}))

        result = self.transport.cancel_order("GRW-2")

        self.assertEqual(result, {"order_id": "GRW-2", "status": "CANCELLED"})
        method, url, _headers, body_bytes = self.captured_calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/v1/order/GRW-2/cancel"))
        self.assertIsNone(body_bytes)

    def test_get_order_sends_expected_get_request_and_parses_response(self) -> None:
        self._install_fake_execute(
            200,
            json.dumps(
                {
                    "order_id": "GRW-3",
                    "status": "FILLED",
                    "symbol": "TCS",
                    "exchange": "NSE",
                    "side": "SELL",
                    "order_type": "LIMIT",
                    "product_type": "DELIVERY",
                    "quantity": 10,
                    "filled_quantity": 10,
                    "average_price": 3800.0,
                }
            ),
        )

        result = self.transport.get_order("GRW-3")

        self.assertEqual(result["symbol"], "TCS")
        self.assertEqual(result["status"], "FILLED")
        method, url, _headers, body_bytes = self.captured_calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/v1/order/GRW-3"))
        self.assertIsNone(body_bytes)

    def test_list_positions_sends_expected_get_request_and_parses_response(self) -> None:
        self._install_fake_execute(
            200,
            json.dumps(
                {
                    "positions": [
                        {
                            "symbol": "RELIANCE",
                            "exchange": "NSE",
                            "quantity": 10,
                            "average_price": 2500.0,
                            "product_type": "INTRADAY",
                        }
                    ]
                }
            ),
        )

        result = self.transport.list_positions()

        self.assertEqual(len(result["positions"]), 1)
        self.assertEqual(result["positions"][0]["symbol"], "RELIANCE")
        method, url, _headers, body_bytes = self.captured_calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/v1/positions"))
        self.assertIsNone(body_bytes)

    def test_base_url_trailing_slash_is_normalized(self) -> None:
        transport = make_transport(base_url="https://api.groww.example/")
        captured = {}

        def fake_execute(method, url, headers, body_bytes):
            captured["url"] = url
            return 200, json.dumps({"positions": []})

        transport._execute_request = fake_execute  # type: ignore[method-assign]
        transport.list_positions()

        self.assertEqual(captured["url"], "https://api.groww.example/v1/positions")


class GrowwHttpTransportErrorPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = make_transport()

    def test_non_2xx_response_raises_groww_api_error(self) -> None:
        self.transport._execute_request = lambda method, url, headers, body: (  # type: ignore[method-assign]
            500,
            json.dumps({"error": "internal_error"}),
        )

        with self.assertRaises(GrowwApiError) as ctx:
            self.transport.list_positions()

        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("internal_error", ctx.exception.response_body)

    def test_401_response_raises_groww_authentication_error(self) -> None:
        self.transport._execute_request = lambda method, url, headers, body: (  # type: ignore[method-assign]
            401,
            json.dumps({"error": "invalid_token"}),
        )

        with self.assertRaises(GrowwAuthenticationError) as ctx:
            self.transport.get_order("GRW-1")

        self.assertEqual(ctx.exception.status_code, 401)

    def test_403_response_raises_groww_authentication_error(self) -> None:
        self.transport._execute_request = lambda method, url, headers, body: (  # type: ignore[method-assign]
            403,
            "{}",
        )

        with self.assertRaises(GrowwAuthenticationError):
            self.transport.cancel_order("GRW-1")

    def test_authentication_error_is_also_an_api_error(self) -> None:
        # GrowwAuthenticationError should be catchable as GrowwApiError too.
        self.transport._execute_request = lambda method, url, headers, body: (401, "{}")  # type: ignore[method-assign]

        with self.assertRaises(GrowwApiError):
            self.transport.get_order("GRW-1")

    def test_connection_failure_raises_groww_transport_error(self) -> None:
        def fake_execute(method, url, headers, body):
            raise GrowwTransportError(f"Groww request failed: {method} {url}: connection refused")

        self.transport._execute_request = fake_execute  # type: ignore[method-assign]

        with self.assertRaises(GrowwTransportError):
            self.transport.place_order({"symbol": "RELIANCE"})

    def test_invalid_json_raises_groww_transport_error(self) -> None:
        self.transport._execute_request = lambda method, url, headers, body: (  # type: ignore[method-assign]
            200,
            "not-valid-json{{{",
        )

        with self.assertRaises(GrowwTransportError):
            self.transport.get_order("GRW-1")

    def test_non_object_json_raises_groww_transport_error(self) -> None:
        self.transport._execute_request = lambda method, url, headers, body: (  # type: ignore[method-assign]
            200,
            json.dumps([1, 2, 3]),
        )

        with self.assertRaises(GrowwTransportError):
            self.transport.list_positions()

    def test_real_execute_request_wraps_malformed_url_as_transport_error(self) -> None:
        # Exercises the *real* urllib-backed _execute_request (not
        # monkeypatched) against a deliberately empty base_url. This is
        # network-safe: urllib rejects a schemeless URL while building the
        # Request object, before any socket/DNS work happens, so this
        # raises synchronously with zero network I/O.
        transport = make_transport(base_url="")

        with self.assertRaises(GrowwTransportError):
            transport.list_positions()


if __name__ == "__main__":
    unittest.main()

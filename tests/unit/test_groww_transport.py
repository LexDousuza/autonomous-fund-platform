"""
Unit tests for core.broker.groww_transport.

These tests never touch a real socket or the real Groww API. Functional
tests inject a fake client shaped like ``growwapi.GrowwAPI`` directly
(via ``GrowwHttpTransport(config, client=...)``), bypassing authentication
entirely - important because, unlike the old raw-HTTP transport, this one
authenticates *at construction time* by calling the real
``GrowwAPI.get_access_token`` (which itself calls a real, hardcoded Groww
URL) unless a client is injected. The authentication exchange itself is
tested separately by mocking ``GrowwAPI.get_access_token`` directly.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_groww_transport.py -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from growwapi import GrowwAPI
from growwapi.groww.exceptions import GrowwAPIAuthenticationException, GrowwAPIBadRequestException

from core.broker.groww_errors import GrowwApiError, GrowwAuthenticationError, GrowwTransportError
from core.broker.groww_models import GrowwCredentials
from core.broker.groww_transport import GrowwHttpTransport, GrowwTransportConfig


class FakeGrowwClient:
    """Test double matching the subset of growwapi.GrowwAPI's interface this transport uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, object] = {}

    def _record(self, name: str, kwargs: dict) -> object:
        self.calls.append((name, kwargs))
        response = self.responses.get(name)
        if isinstance(response, Exception):
            raise response
        return response

    def place_order(self, **kwargs):
        return self._record("place_order", kwargs)

    def cancel_order(self, **kwargs):
        return self._record("cancel_order", kwargs)

    def get_order_detail(self, **kwargs):
        return self._record("get_order_detail", kwargs)

    def get_positions_for_user(self, **kwargs):
        return self._record("get_positions_for_user", kwargs)

    def get_order_margin_details(self, **kwargs):
        return self._record("get_order_margin_details", kwargs)

    def get_available_margin_details(self, **kwargs):
        return self._record("get_available_margin_details", kwargs)


def make_transport(client: FakeGrowwClient | None = None) -> tuple[GrowwHttpTransport, FakeGrowwClient]:
    client = client or FakeGrowwClient()
    config = GrowwTransportConfig(
        credentials=GrowwCredentials(api_key="test-api-key", secret="test-secret"),
        timeout_seconds=5.0,
    )
    return GrowwHttpTransport(config, client=client), client


class GrowwHttpTransportAuthenticationTests(unittest.TestCase):
    @patch("core.broker.groww_transport.generate_totp", return_value="123456")
    @patch.object(GrowwAPI, "get_access_token")
    def test_constructs_authenticated_client_from_api_key_and_totp(
        self, mock_get_token, mock_generate_totp
    ) -> None:
        mock_get_token.return_value = "real-token-value"

        config = GrowwTransportConfig(credentials=GrowwCredentials(api_key="k", secret="s"))
        transport = GrowwHttpTransport(config)

        mock_generate_totp.assert_called_once_with("s")
        mock_get_token.assert_called_once_with(api_key="k", totp="123456")
        self.assertIsInstance(transport._client, GrowwAPI)

    @patch("core.broker.groww_transport.generate_totp", return_value="123456")
    @patch.object(GrowwAPI, "get_access_token")
    def test_authentication_failure_is_translated_to_our_error_hierarchy(
        self, mock_get_token, mock_generate_totp
    ) -> None:
        mock_get_token.side_effect = GrowwAPIAuthenticationException()

        config = GrowwTransportConfig(credentials=GrowwCredentials(api_key="k", secret="bad-secret"))

        with self.assertRaises(GrowwAuthenticationError):
            GrowwHttpTransport(config)

    def test_injected_client_skips_authentication_entirely(self) -> None:
        client = FakeGrowwClient()
        transport, _ = make_transport(client)
        self.assertIs(transport._client, client)


class GrowwHttpTransportSuccessPathTests(unittest.TestCase):
    def test_place_order_forwards_payload_and_fixed_segment_to_the_client(self) -> None:
        transport, client = make_transport()
        client.responses["place_order"] = {"groww_order_id": "GRW-1", "order_status": "NEW"}

        result = transport.place_order(
            {
                "trading_symbol": "RELIANCE",
                "exchange": "NSE",
                "transaction_type": "BUY",
                "quantity": 5,
            }
        )

        self.assertEqual(result, {"groww_order_id": "GRW-1", "order_status": "NEW"})
        name, kwargs = client.calls[0]
        self.assertEqual(name, "place_order")
        self.assertEqual(kwargs["trading_symbol"], "RELIANCE")
        self.assertEqual(kwargs["quantity"], 5)
        self.assertEqual(kwargs["segment"], GrowwAPI.SEGMENT_CASH)

    def test_cancel_order_forwards_order_id_and_segment(self) -> None:
        transport, client = make_transport()
        client.responses["cancel_order"] = {"groww_order_id": "GRW-2", "order_status": "CANCELLED"}

        result = transport.cancel_order("GRW-2")

        self.assertEqual(result["order_status"], "CANCELLED")
        name, kwargs = client.calls[0]
        self.assertEqual(name, "cancel_order")
        self.assertEqual(kwargs["groww_order_id"], "GRW-2")
        self.assertEqual(kwargs["segment"], GrowwAPI.SEGMENT_CASH)

    def test_place_order_forwards_explicit_fno_segment(self) -> None:
        transport, client = make_transport()
        client.responses["place_order"] = {"groww_order_id": "GRW-9", "order_status": "NEW"}

        transport.place_order({"trading_symbol": "RELIANCE26AUG2600CE"}, segment=GrowwAPI.SEGMENT_FNO)

        _, kwargs = client.calls[0]
        self.assertEqual(kwargs["segment"], GrowwAPI.SEGMENT_FNO)

    def test_get_order_calls_get_order_detail_not_get_order_status(self) -> None:
        # get_order_status's verified response shape lacks symbol/exchange/
        # side/order_type/product/quantity - not enough to build an Order.
        transport, client = make_transport()
        client.responses["get_order_detail"] = {"groww_order_id": "GRW-3", "order_status": "EXECUTED"}

        result = transport.get_order("GRW-3")

        self.assertEqual(result["order_status"], "EXECUTED")
        name, kwargs = client.calls[0]
        self.assertEqual(name, "get_order_detail")
        self.assertEqual(kwargs["groww_order_id"], "GRW-3")

    def test_list_positions_scopes_to_cash_segment(self) -> None:
        transport, client = make_transport()
        client.responses["get_positions_for_user"] = {"positions": []}

        result = transport.list_positions()

        self.assertEqual(result, {"positions": []})
        name, kwargs = client.calls[0]
        self.assertEqual(name, "get_positions_for_user")
        self.assertEqual(kwargs["segment"], GrowwAPI.SEGMENT_CASH)

    def test_list_positions_accepts_explicit_fno_segment(self) -> None:
        transport, client = make_transport()
        client.responses["get_positions_for_user"] = {"positions": []}

        transport.list_positions(segment=GrowwAPI.SEGMENT_FNO)

        _, kwargs = client.calls[0]
        self.assertEqual(kwargs["segment"], GrowwAPI.SEGMENT_FNO)

    def test_get_order_margin_details_forwards_orders_and_segment(self) -> None:
        transport, client = make_transport()
        client.responses["get_order_margin_details"] = {"total_requirement": 12500.0}
        orders = [{"trading_symbol": "RELIANCE26AUG2600CE", "quantity": 250}]

        result = transport.get_order_margin_details(orders, segment=GrowwAPI.SEGMENT_FNO)

        self.assertEqual(result, {"total_requirement": 12500.0})
        name, kwargs = client.calls[0]
        self.assertEqual(name, "get_order_margin_details")
        self.assertEqual(kwargs["orders"], orders)
        self.assertEqual(kwargs["segment"], GrowwAPI.SEGMENT_FNO)

    def test_get_available_margin_details_forwards_to_client(self) -> None:
        transport, client = make_transport()
        client.responses["get_available_margin_details"] = {"clear_cash": 50000.0}

        result = transport.get_available_margin_details()

        self.assertEqual(result, {"clear_cash": 50000.0})
        self.assertEqual(client.calls[0][0], "get_available_margin_details")


class GrowwHttpTransportErrorPathTests(unittest.TestCase):
    def test_growwapi_exception_is_translated_to_groww_api_error(self) -> None:
        transport, client = make_transport()
        client.responses["get_positions_for_user"] = GrowwAPIBadRequestException()

        with self.assertRaises(GrowwApiError) as ctx:
            transport.list_positions()

        self.assertEqual(ctx.exception.status_code, 400)

    def test_authentication_style_exception_is_translated_to_authentication_error(self) -> None:
        transport, client = make_transport()
        client.responses["get_order_detail"] = GrowwAPIAuthenticationException()

        with self.assertRaises(GrowwAuthenticationError):
            transport.get_order("GRW-1")

    def test_authentication_error_is_also_an_api_error(self) -> None:
        transport, client = make_transport()
        client.responses["get_order_detail"] = GrowwAPIAuthenticationException()

        with self.assertRaises(GrowwApiError):
            transport.get_order("GRW-1")

    def test_unexpected_exception_is_translated_to_groww_transport_error(self) -> None:
        transport, client = make_transport()
        client.responses["place_order"] = RuntimeError("connection refused")

        with self.assertRaises(GrowwTransportError):
            transport.place_order({"trading_symbol": "RELIANCE"})


class _FlakyThenOkClient(FakeGrowwClient):
    """Fails ``fail_times`` times with a retryable error, then succeeds."""

    def __init__(self, fail_times: int, exc: Exception, ok_response: object) -> None:
        super().__init__()
        self._remaining_failures = fail_times
        self._exc = exc
        self._ok_response = ok_response

    def get_order_detail(self, **kwargs):
        self.calls.append(("get_order_detail", kwargs))
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise self._exc
        return self._ok_response


class _AlwaysBadRequestClient(FakeGrowwClient):
    """Every call fails with a non-retryable 400 - fails fast, no backoff sleep."""

    def get_positions_for_user(self, **kwargs):
        self.calls.append(("get_positions_for_user", kwargs))
        raise GrowwAPIBadRequestException()


class GrowwHttpTransportResilienceTests(unittest.TestCase):
    def test_read_call_retries_transient_failure_then_succeeds(self) -> None:
        client = _FlakyThenOkClient(
            fail_times=1, exc=RuntimeError("timeout"), ok_response={"order_id": "GRW-1"}
        )
        transport, _ = make_transport(client)

        result = transport.get_order("GRW-1")  # must not raise - retries once, then succeeds

        self.assertEqual(result, {"order_id": "GRW-1"})
        self.assertEqual(len(client.calls), 2)

    def test_circuit_opens_after_repeated_failures_and_blocks_further_calls(self) -> None:
        client = _AlwaysBadRequestClient()
        transport, _ = make_transport(client)

        for _ in range(5):  # default CircuitBreaker.failure_threshold
            with self.assertRaises(GrowwApiError):
                transport.list_positions()

        with self.assertRaises(GrowwTransportError) as ctx:
            transport.list_positions()

        self.assertIn("circuit open", str(ctx.exception).lower())
        # the 6th call was blocked by the breaker, not forwarded to the client
        self.assertEqual(len(client.calls), 5)

    def test_write_calls_never_retry(self) -> None:
        client = FakeGrowwClient()
        client.responses["place_order"] = RuntimeError("timeout")
        transport, _ = make_transport(client)

        with self.assertRaises(GrowwTransportError):
            transport.place_order({"trading_symbol": "RELIANCE"})

        self.assertEqual(len(client.calls), 1)  # no retry attempts for a write


if __name__ == "__main__":
    unittest.main()

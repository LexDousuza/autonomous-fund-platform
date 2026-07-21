"""
Unit tests for core.broker.kite_transport.

These tests never touch a real socket or the real Kite API. Functional
tests inject a fake client shaped like ``kiteconnect.KiteConnect``
directly (via ``KiteHttpTransport(config, client=...)``), bypassing
construction-time authentication entirely. The authentication check
itself (``profile()`` called at construction) is tested separately by
patching ``KiteConnect.profile``.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_kite_transport.py -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from kiteconnect import KiteConnect
from kiteconnect.exceptions import GeneralException, InputException, TokenException

from core.broker.kite_errors import KiteApiError, KiteAuthenticationError, KiteTransportError
from core.broker.kite_models import KiteCredentials
from core.broker.kite_transport import KiteHttpTransport, KiteTransportConfig


class FakeKiteClient:
    """Test double matching the subset of kiteconnect.KiteConnect's interface this transport uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, object] = {"profile": {"user_id": "AB1234"}}

    def _record(self, name: str, kwargs: dict) -> object:
        self.calls.append((name, kwargs))
        response = self.responses.get(name)
        if isinstance(response, Exception):
            raise response
        return response

    def profile(self, **kwargs):
        return self._record("profile", kwargs)

    def place_order(self, **kwargs):
        return self._record("place_order", kwargs)

    def cancel_order(self, **kwargs):
        return self._record("cancel_order", kwargs)

    def order_history(self, **kwargs):
        return self._record("order_history", kwargs)

    def positions(self, **kwargs):
        return self._record("positions", kwargs)


def make_transport(client: FakeKiteClient | None = None) -> tuple[KiteHttpTransport, FakeKiteClient]:
    client = client or FakeKiteClient()
    config = KiteTransportConfig(
        credentials=KiteCredentials(api_key="test-api-key", access_token="test-access-token"),
        timeout_seconds=5.0,
    )
    return KiteHttpTransport(config, client=client), client


class KiteHttpTransportAuthenticationTests(unittest.TestCase):
    @patch.object(KiteConnect, "profile")
    def test_construction_validates_access_token_with_profile_call(self, mock_profile) -> None:
        mock_profile.return_value = {"user_id": "AB1234"}

        config = KiteTransportConfig(credentials=KiteCredentials(api_key="k", access_token="t"))
        transport = KiteHttpTransport(config)

        mock_profile.assert_called_once()
        self.assertIsInstance(transport._client, KiteConnect)

    @patch.object(KiteConnect, "profile")
    def test_expired_token_at_construction_is_translated_to_our_error_hierarchy(self, mock_profile) -> None:
        mock_profile.side_effect = TokenException("token expired")

        config = KiteTransportConfig(credentials=KiteCredentials(api_key="k", access_token="stale-token"))

        with self.assertRaises(KiteAuthenticationError):
            KiteHttpTransport(config)

    def test_injected_client_skips_authentication_entirely(self) -> None:
        client = FakeKiteClient()
        transport, _ = make_transport(client)
        self.assertIs(transport._client, client)
        self.assertEqual(client.calls, [])  # profile() only called by real _authenticate, not skipped here


class KiteHttpTransportSuccessPathTests(unittest.TestCase):
    def test_place_order_forwards_payload_and_fixed_variety_to_the_client(self) -> None:
        transport, client = make_transport()
        client.responses["place_order"] = "KITE-1"

        result = transport.place_order(
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "transaction_type": "BUY",
                "quantity": 5,
            }
        )

        self.assertEqual(result, {"order_id": "KITE-1"})
        name, kwargs = client.calls[0]
        self.assertEqual(name, "place_order")
        self.assertEqual(kwargs["tradingsymbol"], "RELIANCE")
        self.assertEqual(kwargs["quantity"], 5)
        self.assertEqual(kwargs["variety"], "regular")

    def test_cancel_order_forwards_order_id_and_variety(self) -> None:
        transport, client = make_transport()
        client.responses["cancel_order"] = "KITE-2"

        result = transport.cancel_order("KITE-2")

        self.assertEqual(result, {"order_id": "KITE-2"})
        name, kwargs = client.calls[0]
        self.assertEqual(name, "cancel_order")
        self.assertEqual(kwargs["order_id"], "KITE-2")
        self.assertEqual(kwargs["variety"], "regular")

    def test_get_order_returns_the_last_history_entry(self) -> None:
        transport, client = make_transport()
        client.responses["order_history"] = [
            {"order_id": "KITE-3", "status": "OPEN"},
            {"order_id": "KITE-3", "status": "COMPLETE"},
        ]

        result = transport.get_order("KITE-3")

        self.assertEqual(result, {"order_id": "KITE-3", "status": "COMPLETE"})
        name, kwargs = client.calls[0]
        self.assertEqual(name, "order_history")
        self.assertEqual(kwargs["order_id"], "KITE-3")

    def test_get_order_raises_on_empty_history(self) -> None:
        transport, client = make_transport()
        client.responses["order_history"] = []

        with self.assertRaises(KiteTransportError):
            transport.get_order("KITE-missing")

    def test_list_positions_returns_raw_net_and_day_shape(self) -> None:
        transport, client = make_transport()
        client.responses["positions"] = {"net": [{"tradingsymbol": "RELIANCE"}], "day": []}

        result = transport.list_positions()

        self.assertEqual(result, {"net": [{"tradingsymbol": "RELIANCE"}], "day": []})


class KiteHttpTransportErrorPathTests(unittest.TestCase):
    def test_kite_exception_is_translated_to_kite_api_error(self) -> None:
        transport, client = make_transport()
        client.responses["positions"] = GeneralException("server error", code=500)

        with self.assertRaises(KiteApiError) as ctx:
            transport.list_positions()

        self.assertEqual(ctx.exception.status_code, 500)

    def test_token_exception_is_translated_to_authentication_error(self) -> None:
        transport, client = make_transport()
        client.responses["order_history"] = TokenException("session expired")

        with self.assertRaises(KiteAuthenticationError):
            transport.get_order("KITE-1")

    def test_authentication_error_is_also_an_api_error(self) -> None:
        transport, client = make_transport()
        client.responses["order_history"] = TokenException("session expired")

        with self.assertRaises(KiteApiError):
            transport.get_order("KITE-1")

    def test_unexpected_exception_is_translated_to_kite_transport_error(self) -> None:
        transport, client = make_transport()
        client.responses["place_order"] = RuntimeError("connection refused")

        with self.assertRaises(KiteTransportError):
            transport.place_order({"tradingsymbol": "RELIANCE"})


class _FlakyThenOkClient(FakeKiteClient):
    """Fails ``fail_times`` times with a retryable error, then succeeds."""

    def __init__(self, fail_times: int, exc: Exception, ok_response: object) -> None:
        super().__init__()
        self._remaining_failures = fail_times
        self._exc = exc
        self._ok_response = ok_response

    def order_history(self, **kwargs):
        self.calls.append(("order_history", kwargs))
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise self._exc
        return self._ok_response


class _AlwaysBadRequestClient(FakeKiteClient):
    """Every call fails with a non-retryable 400 - fails fast, no backoff sleep."""

    def positions(self, **kwargs):
        self.calls.append(("positions", kwargs))
        raise InputException("bad request", code=400)


class KiteHttpTransportResilienceTests(unittest.TestCase):
    def test_read_call_retries_transient_failure_then_succeeds(self) -> None:
        client = _FlakyThenOkClient(
            fail_times=1,
            exc=RuntimeError("timeout"),
            ok_response=[{"order_id": "KITE-1", "status": "COMPLETE"}],
        )
        transport, _ = make_transport(client)

        result = transport.get_order("KITE-1")  # must not raise - retries once, then succeeds

        self.assertEqual(result, {"order_id": "KITE-1", "status": "COMPLETE"})
        self.assertEqual(len(client.calls), 2)

    def test_circuit_opens_after_repeated_failures_and_blocks_further_calls(self) -> None:
        client = _AlwaysBadRequestClient()
        transport, _ = make_transport(client)

        for _ in range(5):  # default CircuitBreaker.failure_threshold
            with self.assertRaises(KiteApiError):
                transport.list_positions()

        with self.assertRaises(KiteTransportError) as ctx:
            transport.list_positions()

        self.assertIn("circuit open", str(ctx.exception).lower())
        self.assertEqual(len(client.calls), 5)  # the 6th call was blocked, not forwarded

    def test_write_calls_never_retry(self) -> None:
        client = FakeKiteClient()
        client.responses["place_order"] = RuntimeError("timeout")
        transport, _ = make_transport(client)

        with self.assertRaises(KiteTransportError):
            transport.place_order({"tradingsymbol": "RELIANCE"})

        self.assertEqual(len(client.calls), 1)  # no retry attempts for a write


if __name__ == "__main__":
    unittest.main()

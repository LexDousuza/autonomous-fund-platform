"""
Unit tests for core.market_data.angelone_transport.

These tests never touch a real socket, the real SmartAPI, or the real
scrip master URL: a fake client double (matching the subset of
SmartApi.SmartConnect's interface this transport uses) is injected via
AngelOneMarketDataTransport(config, client=...), bypassing construction-
time authentication entirely. The scrip master fetch is patched at the
module level (``angelone_transport._load_scrip_master``'s ``fetch``
inner function isn't directly patchable, so tests patch
``urllib.request.urlopen`` instead - see ``_patched_scrip_master``).

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_angelone_transport.py -v
"""
from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import patch

from SmartApi.smartExceptions import GeneralException, TokenException

from core.market_data.angelone_transport import (
    AngelOneMarketDataCredentials,
    AngelOneMarketDataTransport,
    AngelOneMarketDataTransportConfig,
)
from core.market_data.errors import (
    MarketDataAuthenticationError,
    MarketDataProviderError,
    MarketDataTransportError,
    UnsupportedSymbolError,
)


class FakeSmartConnectClient:
    """Test double matching the subset of SmartApi.SmartConnect's interface this transport uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.session_response: object = {
            "status": True,
            "data": {"jwtToken": "jwt", "refreshToken": "refresh", "feedToken": "feed"},
        }
        self.candle_response: object = {"status": True, "message": "SUCCESS", "errorcode": "", "data": []}

    def generateSession(self, client_code, password, totp):
        self.calls.append(("generateSession", {"client_code": client_code, "password": password, "totp": totp}))
        response = self.session_response
        if isinstance(response, Exception):
            raise response
        return response

    def getCandleData(self, params):
        self.calls.append(("getCandleData", params))
        response = self.candle_response
        if isinstance(response, Exception):
            raise response
        return response


_SCRIP_MASTER_FIXTURE = [
    {"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "instrumenttype": "", "exch_seg": "NSE"},
    {"token": "1594", "symbol": "INFY-EQ", "name": "INFY", "instrumenttype": "", "exch_seg": "NSE"},
    # A derivative row sharing RELIANCE's name - must never be matched for cash-equity lookups.
    {"token": "99999", "symbol": "RELIANCE25JANFUT", "name": "RELIANCE", "instrumenttype": "FUTSTK", "exch_seg": "NFO"},
]


@contextmanager
def _patched_scrip_master(rows: list[dict] = _SCRIP_MASTER_FIXTURE):
    body = json.dumps(rows).encode("utf-8")

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return body

    with patch("urllib.request.urlopen", return_value=_FakeResponse()):
        yield


def make_transport(client: FakeSmartConnectClient | None = None) -> tuple[AngelOneMarketDataTransport, FakeSmartConnectClient]:
    client = client or FakeSmartConnectClient()
    config = AngelOneMarketDataTransportConfig(
        credentials=AngelOneMarketDataCredentials(
            api_key="key", client_code="C123", password="1234", totp_secret="JBSWY3DPEHPK3PXP"
        )
    )
    return AngelOneMarketDataTransport(config, client=client), client


class AngelOneMarketDataTransportAuthenticationTests(unittest.TestCase):
    def test_injected_client_skips_authentication_entirely(self) -> None:
        client = FakeSmartConnectClient()
        transport, _ = make_transport(client)
        self.assertIs(transport._client, client)
        self.assertEqual(client.calls, [])  # generateSession only called by real _authenticate, not here

    def test_construction_computes_a_live_totp_and_calls_generate_session(self) -> None:
        # Real construction path (no injected client) - generateSession must actually run.
        client_holder: list[FakeSmartConnectClient] = []

        def fake_smart_connect(**kwargs):
            fake = FakeSmartConnectClient()
            client_holder.append(fake)
            return fake

        with patch("core.market_data.angelone_transport.SmartConnect", side_effect=fake_smart_connect):
            config = AngelOneMarketDataTransportConfig(
                credentials=AngelOneMarketDataCredentials(
                    api_key="key", client_code="C123", password="1234", totp_secret="JBSWY3DPEHPK3PXP"
                )
            )
            AngelOneMarketDataTransport(config)

        name, kwargs = client_holder[0].calls[0]
        self.assertEqual(name, "generateSession")
        self.assertEqual(kwargs["client_code"], "C123")
        self.assertEqual(kwargs["password"], "1234")
        self.assertEqual(len(kwargs["totp"]), 6)
        self.assertTrue(kwargs["totp"].isdigit())

    def _construct_with_fake_session_response(self, session_response: object) -> None:
        # Injecting client= (as make_transport() does) skips _authenticate()
        # entirely (see test_injected_client_skips_authentication_entirely),
        # so exercising the auth-failure path needs a real construction with
        # SmartConnect() itself patched, mirroring how test_kite_transport.py
        # patches KiteConnect.profile for the equivalent case.
        fake = FakeSmartConnectClient()
        fake.session_response = session_response
        config = AngelOneMarketDataTransportConfig(
            credentials=AngelOneMarketDataCredentials(
                api_key="key", client_code="C123", password="1234", totp_secret="JBSWY3DPEHPK3PXP"
            )
        )
        with patch("core.market_data.angelone_transport.SmartConnect", return_value=fake):
            AngelOneMarketDataTransport(config)

    def test_failed_login_without_error_type_raises_authentication_error(self) -> None:
        # Verified SDK behavior: generateSession returns {"status": False, ...} rather
        # than raising, for a plain wrong-password/TOTP/client-code failure.
        with self.assertRaises(MarketDataAuthenticationError):
            self._construct_with_fake_session_response(
                {"status": False, "message": "Invalid Credentials", "errorcode": "AB1010"}
            )

    def test_raised_token_exception_during_login_is_translated(self) -> None:
        with self.assertRaises(MarketDataAuthenticationError):
            self._construct_with_fake_session_response(TokenException("bad totp"))


class AngelOneMarketDataTransportHistoricalTests(unittest.TestCase):
    def test_get_historical_candles_resolves_symbol_token_and_forwards_params(self) -> None:
        transport, client = make_transport()
        client.candle_response = {
            "status": True,
            "message": "SUCCESS",
            "errorcode": "",
            "data": [["2026-01-01T09:15:00+05:30", 100.0, 105.0, 95.0, 102.0, 1000]],
        }

        with _patched_scrip_master():
            result = transport.get_historical_candles(
                "RELIANCE", "NSE", "ONE_DAY", "2026-01-01 09:15", "2026-01-31 15:30"
            )

        self.assertEqual(result, client.candle_response["data"])
        name, params = [c for c in client.calls if c[0] == "getCandleData"][0]
        self.assertEqual(params["symboltoken"], "2885")
        self.assertEqual(params["exchange"], "NSE")
        self.assertEqual(params["interval"], "ONE_DAY")
        self.assertEqual(params["fromdate"], "2026-01-01 09:15")
        self.assertEqual(params["todate"], "2026-01-31 15:30")

    def test_scrip_master_is_fetched_once_and_cached_across_calls(self) -> None:
        transport, client = make_transport()

        with _patched_scrip_master() as _, patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda self: self
            mock_urlopen.return_value.__exit__ = lambda self, *a: False
            mock_urlopen.return_value.read.return_value = json.dumps(_SCRIP_MASTER_FIXTURE).encode()

            transport.get_historical_candles("RELIANCE", "NSE", "ONE_DAY", "2026-01-01 09:15", "2026-01-31 15:30")
            transport.get_historical_candles("RELIANCE", "NSE", "ONE_DAY", "2026-02-01 09:15", "2026-02-28 15:30")

            self.assertEqual(mock_urlopen.call_count, 1)  # fetched once, reused for the second call

    def test_derivative_rows_never_match_cash_equity_lookup(self) -> None:
        transport, _ = make_transport()

        with _patched_scrip_master():
            with self.assertRaises(UnsupportedSymbolError):
                # RELIANCE exists on NFO in the fixture too, but only as a FUTSTK row -
                # a cash-equity NFO lookup must not resolve to it.
                transport.get_historical_candles("RELIANCE", "NFO", "ONE_DAY", "2026-01-01 09:15", "2026-01-31 15:30")

    def test_unresolvable_symbol_raises_unsupported_symbol_error(self) -> None:
        transport, _ = make_transport()

        with _patched_scrip_master():
            with self.assertRaises(UnsupportedSymbolError):
                transport.get_historical_candles("UNKNOWN", "NSE", "ONE_DAY", "2026-01-01 09:15", "2026-01-31 15:30")

    def test_status_false_response_without_error_type_raises_provider_error(self) -> None:
        transport, client = make_transport()
        client.candle_response = {"status": False, "message": "bad request", "errorcode": "AB2000", "data": None}

        with _patched_scrip_master():
            with self.assertRaises(MarketDataProviderError):
                transport.get_historical_candles("RELIANCE", "NSE", "ONE_DAY", "2026-01-01 09:15", "2026-01-31 15:30")

    def test_raised_general_exception_is_translated_to_provider_error(self) -> None:
        transport, client = make_transport()
        client.candle_response = GeneralException("server error", code=500)

        with _patched_scrip_master():
            with self.assertRaises(MarketDataProviderError):
                transport.get_historical_candles("RELIANCE", "NSE", "ONE_DAY", "2026-01-01 09:15", "2026-01-31 15:30")

    def test_unexpected_exception_is_translated_to_transport_error(self) -> None:
        transport, client = make_transport()
        client.candle_response = RuntimeError("connection refused")

        with _patched_scrip_master():
            with self.assertRaises(MarketDataTransportError):
                transport.get_historical_candles("RELIANCE", "NSE", "ONE_DAY", "2026-01-01 09:15", "2026-01-31 15:30")


if __name__ == "__main__":
    unittest.main()

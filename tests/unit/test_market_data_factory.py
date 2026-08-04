"""
Unit tests for core.market_data.factory.

Building a real Groww feed authenticates immediately (the transport
exchanges api_key+secret for an access token at construction - see
core/market_data/groww_transport.py), so every test in this class mocks
growwapi.GrowwAPI.get_access_token to avoid a real call to Groww's real
auth endpoint.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_market_data_factory.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from growwapi import GrowwAPI

from core.market_data.angelone_historical_feed import AngelOneHistoricalMarketDataFeed
from core.market_data.angelone_intraday_feed import AngelOneIntradayLiveMarketDataFeed
from core.market_data.angelone_live_feed import AngelOneLiveMarketDataFeed
from core.market_data.angelone_transport import AngelOneMarketDataTransport
from core.market_data.csv_feed import CSVMarketDataFeed
from core.market_data.factory import build_market_data_feed
from core.market_data.factory_models import MarketDataConfig
from core.market_data.groww_historical_feed import GrowwHistoricalMarketDataFeed
from core.market_data.groww_live_feed import GrowwLiveMarketDataFeed
from core.market_data.groww_transport import GrowwMarketDataTransport
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.strategy.base import MarketBar

CSV_PATH = "data/backtests/sample_reliance_1m.csv"


def make_bar(symbol: str = "AAA", close: float = 100.0) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        exchange="NSE",
        timestamp=datetime(2026, 1, 1, 9, 15, 0),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000.0,
    )


@patch("core.market_data.groww_transport.generate_totp", return_value="123456")
@patch.object(GrowwAPI, "get_access_token", return_value="fake-token")
class BuildMarketDataFeedTests(unittest.TestCase):
    def test_memory_config_returns_memory_feed(self, mock_get_token, mock_generate_totp) -> None:
        feed = build_market_data_feed(MarketDataConfig(provider="memory", mode="paper"))

        self.assertIsInstance(feed, MemoryMarketDataFeed)
        mock_get_token.assert_not_called()

    def test_memory_config_seeds_bars_from_metadata(self, mock_get_token, mock_generate_totp) -> None:
        bar = make_bar()
        feed = build_market_data_feed(
            MarketDataConfig(provider="memory", mode="paper", metadata={"bars": [bar]})
        )

        self.assertEqual(feed.get_latest_bar("AAA"), bar)

    def test_csv_config_returns_csv_feed(self, mock_get_token, mock_generate_totp) -> None:
        feed = build_market_data_feed(
            MarketDataConfig(provider="csv", mode="paper", metadata={"path": CSV_PATH})
        )

        self.assertIsInstance(feed, CSVMarketDataFeed)
        self.assertGreater(len(feed.get_history("RELIANCE")), 0)

    def test_csv_config_without_path_raises(self, mock_get_token, mock_generate_totp) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(MarketDataConfig(provider="csv", mode="paper"))

    def test_groww_live_config_returns_groww_live_feed(self, mock_get_token, mock_generate_totp) -> None:
        feed = build_market_data_feed(
            MarketDataConfig(
                provider="groww",
                mode="live",
                metadata={"api_key": "key", "secret": "secret"},
            )
        )

        self.assertIsInstance(feed, GrowwLiveMarketDataFeed)

    def test_groww_paper_mode_raises(self, mock_get_token, mock_generate_totp) -> None:
        # V1: no verified separate Groww paper/sandbox market-data mode.
        with self.assertRaises(ValueError):
            build_market_data_feed(
                MarketDataConfig(
                    provider="groww",
                    mode="paper",
                    metadata={"api_key": "key", "secret": "secret"},
                )
            )

    def test_groww_without_credentials_raises(self, mock_get_token, mock_generate_totp) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(MarketDataConfig(provider="groww", mode="live"))

    def test_groww_config_passes_through_settings(self, mock_get_token, mock_generate_totp) -> None:
        feed = build_market_data_feed(
            MarketDataConfig(
                provider="groww",
                mode="live",
                metadata={
                    "api_key": "key",
                    "secret": "secret",
                    "exchange": "BSE",
                    "poll_interval_seconds": 1.5,
                    "max_polls": 3,
                    "timeout_seconds": 20.0,
                },
            )
        )

        self.assertEqual(feed._exchange, "BSE")
        self.assertEqual(feed._settings.poll_interval_seconds, 1.5)
        self.assertEqual(feed._settings.max_polls, 3)
        self.assertEqual(feed._transport._config.timeout_seconds, 20.0)
        mock_generate_totp.assert_called_once_with("secret")
        mock_get_token.assert_called_once_with(api_key="key", totp="123456")

    def test_groww_config_uses_safe_default_bounded_polls(self, mock_get_token, mock_generate_totp) -> None:
        feed = build_market_data_feed(
            MarketDataConfig(
                provider="groww",
                mode="live",
                metadata={"api_key": "key", "secret": "secret"},
            )
        )

        self.assertEqual(feed._settings.max_polls, 1)

    def test_groww_historical_without_instruments_raises(self, mock_get_token, mock_generate_totp) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(
                MarketDataConfig(
                    provider="groww",
                    mode="historical",
                    metadata={
                        "api_key": "key",
                        "secret": "secret",
                        "from_date": "2026-01-01",
                        "to_date": "2026-01-31",
                    },
                )
            )

    def test_groww_historical_without_date_range_raises(self, mock_get_token, mock_generate_totp) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(
                MarketDataConfig(
                    provider="groww",
                    mode="historical",
                    metadata={
                        "api_key": "key",
                        "secret": "secret",
                        "instruments": [{"symbol": "RELIANCE", "exchange": "NSE"}],
                    },
                )
            )

    def test_groww_paper_mode_still_rejected_for_historical_provider(self, mock_get_token, mock_generate_totp) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(MarketDataConfig(provider="groww", mode="paper"))

    @patch.object(GrowwMarketDataTransport, "get_historical_candles")
    def test_groww_historical_config_builds_feed_with_expected_settings(
        self, mock_fetch, mock_get_token, mock_generate_totp
    ) -> None:
        mock_fetch.return_value = {
            "candles": [[1767250800, 100.0, 101.0, 99.0, 100.5, 1000.0]]  # verified array shape
        }

        feed = build_market_data_feed(
            MarketDataConfig(
                provider="groww",
                mode="historical",
                metadata={
                    "api_key": "key",
                    "secret": "secret",
                    "instruments": [
                        {"symbol": "RELIANCE", "exchange": "NSE"},
                        {"symbol": "TCS", "exchange": "NSE"},
                    ],
                    "from_date": "2026-01-01",
                    "to_date": "2026-01-31",
                    "interval": "1day",
                },
            )
        )

        self.assertIsInstance(feed, GrowwHistoricalMarketDataFeed)
        self.assertEqual(mock_fetch.call_count, 2)
        mock_fetch.assert_any_call("RELIANCE", "NSE", "1day", "2026-01-01 09:15:00", "2026-01-31 15:30:00")
        mock_fetch.assert_any_call("TCS", "NSE", "1day", "2026-01-01 09:15:00", "2026-01-31 15:30:00")
        self.assertEqual(len(feed.get_history("RELIANCE")), 1)

    def test_unsupported_provider_raises(self, mock_get_token, mock_generate_totp) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(MarketDataConfig(provider="polygon", mode="live"))

    def test_unsupported_mode_raises(self, mock_get_token, mock_generate_totp) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(MarketDataConfig(provider="memory", mode="sandbox"))

    def test_provider_and_mode_are_case_insensitive(self, mock_get_token, mock_generate_totp) -> None:
        feed = build_market_data_feed(MarketDataConfig(provider="MEMORY", mode="Paper"))

        self.assertIsInstance(feed, MemoryMarketDataFeed)


class FakeAngelOneSmartConnect:
    """Test double matching the subset of SmartApi.SmartConnect's interface build_market_data_feed exercises."""

    def generateSession(self, client_code, password, totp):
        return {"status": True, "data": {"jwtToken": "jwt", "refreshToken": "refresh", "feedToken": "feed"}}


_ANGELONE_METADATA = {
    "api_key": "key",
    "client_code": "C123",
    "password": "1234",
    "totp_secret": "JBSWY3DPEHPK3PXP",
    "instruments": [{"symbol": "RELIANCE", "exchange": "NSE"}],
    "from_date": "2026-01-01",
    "to_date": "2026-01-31",
}


@patch("core.market_data.angelone_transport.SmartConnect", return_value=FakeAngelOneSmartConnect())
class BuildAngelOneMarketDataFeedTests(unittest.TestCase):
    """
    Building a real Angel One feed authenticates immediately (see
    AngelOneMarketDataTransport._authenticate), so every test here mocks
    SmartConnect itself - the class-level patch avoids a real call to
    Angel One's real auth endpoint.
    """

    def test_angelone_live_config_returns_angelone_live_feed(self, mock_smart_connect) -> None:
        feed = build_market_data_feed(
            MarketDataConfig(provider="angelone", mode="live", metadata=_ANGELONE_METADATA)
        )

        self.assertIsInstance(feed, AngelOneLiveMarketDataFeed)

    def test_angelone_live_candles_config_returns_angelone_intraday_feed(self, mock_smart_connect) -> None:
        feed = build_market_data_feed(
            MarketDataConfig(provider="angelone", mode="live_candles", metadata=_ANGELONE_METADATA)
        )

        self.assertIsInstance(feed, AngelOneIntradayLiveMarketDataFeed)

    def test_angelone_paper_mode_raises(self, mock_smart_connect) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(
                MarketDataConfig(provider="angelone", mode="paper", metadata=_ANGELONE_METADATA)
            )

    def test_angelone_without_credentials_raises(self, mock_smart_connect) -> None:
        with self.assertRaises(ValueError):
            build_market_data_feed(MarketDataConfig(provider="angelone", mode="historical"))

    def test_angelone_historical_without_instruments_raises(self, mock_smart_connect) -> None:
        metadata = {k: v for k, v in _ANGELONE_METADATA.items() if k != "instruments"}
        with self.assertRaises(ValueError):
            build_market_data_feed(MarketDataConfig(provider="angelone", mode="historical", metadata=metadata))

    def test_angelone_historical_without_date_range_raises(self, mock_smart_connect) -> None:
        metadata = {k: v for k, v in _ANGELONE_METADATA.items() if k not in ("from_date", "to_date")}
        with self.assertRaises(ValueError):
            build_market_data_feed(MarketDataConfig(provider="angelone", mode="historical", metadata=metadata))

    @patch.object(AngelOneMarketDataTransport, "get_historical_candles")
    def test_angelone_historical_config_builds_feed_with_expected_settings(
        self, mock_fetch, mock_smart_connect
    ) -> None:
        mock_fetch.return_value = [["2026-01-01T09:15:00+05:30", 100.0, 101.0, 99.0, 100.5, 1000]]

        feed = build_market_data_feed(
            MarketDataConfig(provider="angelone", mode="historical", metadata=_ANGELONE_METADATA)
        )

        self.assertIsInstance(feed, AngelOneHistoricalMarketDataFeed)
        mock_fetch.assert_called_once_with(
            "RELIANCE", "NSE", "ONE_DAY", "2026-01-01 09:15", "2026-01-31 15:30"
        )
        self.assertEqual(len(feed.get_history("RELIANCE")), 1)


if __name__ == "__main__":
    unittest.main()

"""
Unit tests for core.broker.factory.

Building a real GrowwBrokerAdapter authenticates immediately (the
adapter's transport exchanges api_key+secret for an access token at
construction - see core/broker/groww_transport.py), so every test that
builds one mocks growwapi.GrowwAPI.get_access_token to avoid a real call
to Groww's real auth endpoint.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_broker_factory.py -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from growwapi import GrowwAPI

from core.broker.factory import build_broker
from core.broker.factory_models import BrokerConfig
from core.broker.groww_adapter import GrowwBrokerAdapter
from core.broker.paper import PaperBroker


@patch("core.broker.groww_transport.generate_totp", return_value="123456")
@patch.object(GrowwAPI, "get_access_token", return_value="fake-token")
class BuildBrokerTests(unittest.TestCase):
    def test_paper_config_returns_paper_broker(self, mock_get_token, mock_generate_totp) -> None:
        broker = build_broker(BrokerConfig(provider="paper", mode="paper"))

        self.assertIsInstance(broker, PaperBroker)
        mock_get_token.assert_not_called()

    def test_groww_paper_config_returns_groww_adapter_in_paper_mode(self, mock_get_token, mock_generate_totp) -> None:
        broker = build_broker(
            BrokerConfig(
                provider="groww",
                mode="paper",
                metadata={"api_key": "key", "secret": "secret"},
            )
        )

        self.assertIsInstance(broker, GrowwBrokerAdapter)
        self.assertEqual(broker.mode, "paper")

    def test_groww_live_config_returns_groww_adapter_in_live_mode(self, mock_get_token, mock_generate_totp) -> None:
        broker = build_broker(
            BrokerConfig(
                provider="groww",
                mode="live",
                metadata={"api_key": "key", "secret": "secret"},
            )
        )

        self.assertIsInstance(broker, GrowwBrokerAdapter)
        self.assertEqual(broker.mode, "live")

    def test_groww_config_passes_through_credentials(self, mock_get_token, mock_generate_totp) -> None:
        broker = build_broker(
            BrokerConfig(
                provider="groww",
                mode="live",
                metadata={"api_key": "my-key", "secret": "my-secret"},
            )
        )

        self.assertEqual(broker._credentials.api_key, "my-key")
        self.assertEqual(broker._credentials.secret, "my-secret")
        mock_generate_totp.assert_called_once_with("my-secret")
        mock_get_token.assert_called_once_with(api_key="my-key", totp="123456")

    def test_groww_config_passes_through_timeout_seconds(self, mock_get_token, mock_generate_totp) -> None:
        broker = build_broker(
            BrokerConfig(
                provider="groww",
                mode="live",
                metadata={"api_key": "key", "secret": "secret", "timeout_seconds": 30.0},
            )
        )

        self.assertEqual(broker._transport._config.timeout_seconds, 30.0)

    def test_groww_config_defaults_timeout_seconds(self, mock_get_token, mock_generate_totp) -> None:
        broker = build_broker(
            BrokerConfig(
                provider="groww",
                mode="live",
                metadata={"api_key": "key", "secret": "secret"},
            )
        )

        self.assertEqual(broker._transport._config.timeout_seconds, 10.0)

    def test_unsupported_provider_raises(self, mock_get_token, mock_generate_totp) -> None:
        with self.assertRaises(ValueError):
            build_broker(BrokerConfig(provider="zerodha", mode="paper"))

    def test_unsupported_mode_raises(self, mock_get_token, mock_generate_totp) -> None:
        with self.assertRaises(ValueError):
            build_broker(BrokerConfig(provider="groww", mode="sandbox"))

    def test_paper_provider_with_live_mode_raises(self, mock_get_token, mock_generate_totp) -> None:
        with self.assertRaises(ValueError):
            build_broker(BrokerConfig(provider="paper", mode="live"))

    def test_groww_without_credentials_raises(self, mock_get_token, mock_generate_totp) -> None:
        with self.assertRaises(ValueError):
            build_broker(BrokerConfig(provider="groww", mode="paper"))

    def test_provider_is_case_insensitive(self, mock_get_token, mock_generate_totp) -> None:
        broker = build_broker(BrokerConfig(provider="PAPER", mode="Paper"))

        self.assertIsInstance(broker, PaperBroker)


if __name__ == "__main__":
    unittest.main()

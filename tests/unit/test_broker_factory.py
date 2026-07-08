"""
Unit tests for core.broker.factory.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_broker_factory.py -v
"""
from __future__ import annotations

import unittest

from core.broker.factory import build_broker
from core.broker.factory_models import BrokerConfig
from core.broker.groww_adapter import GrowwBrokerAdapter
from core.broker.paper import PaperBroker


class BuildBrokerTests(unittest.TestCase):
    def test_paper_config_returns_paper_broker(self) -> None:
        broker = build_broker(BrokerConfig(provider="paper", mode="paper"))

        self.assertIsInstance(broker, PaperBroker)

    def test_groww_paper_config_returns_groww_adapter_in_paper_mode(self) -> None:
        broker = build_broker(
            BrokerConfig(
                provider="groww",
                mode="paper",
                metadata={"api_key": "key", "access_token": "token"},
            )
        )

        self.assertIsInstance(broker, GrowwBrokerAdapter)
        self.assertEqual(broker.mode, "paper")

    def test_groww_live_config_returns_groww_adapter_in_live_mode(self) -> None:
        broker = build_broker(
            BrokerConfig(
                provider="groww",
                mode="live",
                metadata={"api_key": "key", "access_token": "token"},
            )
        )

        self.assertIsInstance(broker, GrowwBrokerAdapter)
        self.assertEqual(broker.mode, "live")

    def test_groww_config_passes_through_credentials_and_base_url(self) -> None:
        broker = build_broker(
            BrokerConfig(
                provider="groww",
                mode="live",
                metadata={
                    "api_key": "my-key",
                    "access_token": "my-token",
                    "base_url": "https://api.groww.example",
                },
            )
        )

        self.assertEqual(broker._credentials.api_key, "my-key")
        self.assertEqual(broker._credentials.access_token, "my-token")
        self.assertEqual(broker._base_url, "https://api.groww.example")

    def test_groww_config_passes_through_timeout_seconds(self) -> None:
        broker = build_broker(
            BrokerConfig(
                provider="groww",
                mode="live",
                metadata={"api_key": "key", "access_token": "token", "timeout_seconds": 30.0},
            )
        )

        self.assertEqual(broker._transport._config.timeout_seconds, 30.0)

    def test_groww_config_defaults_timeout_seconds(self) -> None:
        broker = build_broker(
            BrokerConfig(
                provider="groww",
                mode="live",
                metadata={"api_key": "key", "access_token": "token"},
            )
        )

        self.assertEqual(broker._transport._config.timeout_seconds, 10.0)

    def test_unsupported_provider_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_broker(BrokerConfig(provider="zerodha", mode="paper"))

    def test_unsupported_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_broker(BrokerConfig(provider="groww", mode="sandbox"))

    def test_paper_provider_with_live_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_broker(BrokerConfig(provider="paper", mode="live"))

    def test_groww_without_credentials_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_broker(BrokerConfig(provider="groww", mode="paper"))

    def test_provider_is_case_insensitive(self) -> None:
        broker = build_broker(BrokerConfig(provider="PAPER", mode="Paper"))

        self.assertIsInstance(broker, PaperBroker)


if __name__ == "__main__":
    unittest.main()

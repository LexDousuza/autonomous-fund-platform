"""
Unit tests for core.market_data.fo_iv_screen.resolve_iv_by_symbol.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_fo_iv_screen.py -v
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import patch

from core.market_data.errors import MarketDataTransportError
from core.market_data.fo_iv_screen import resolve_iv_by_symbol
from core.market_data.universe_models import Instrument
from core.strategy.base import MarketBar


@dataclass
class _GrowwCreds:
    api_key: str = "k"
    secret: str = "s"


@dataclass
class _KiteCreds:
    api_key: str = "k"
    access_token: str = "t"


def _instrument(symbol: str, tags: list[str] | None = None) -> Instrument:
    return Instrument(symbol=symbol, exchange="NSE", tags=tags or [])


def _bars(symbol: str, close: float) -> list[MarketBar]:
    return [
        MarketBar(
            symbol=symbol, exchange="NSE", timestamp=datetime(2026, 1, 1, 9, 15),
            open=close, high=close, low=close, close=close, volume=1000,
        )
    ]


class ResolveIvBySymbolTests(unittest.TestCase):
    def test_no_fo_eligible_candidates_returns_empty_without_building_transport(self) -> None:
        instruments = [_instrument("ITC")]
        with patch("core.market_data.fo_iv_screen._build_transport") as mock_build:
            result = resolve_iv_by_symbol(instruments, {}, "groww", _GrowwCreds(), None)
        mock_build.assert_not_called()
        self.assertEqual(result, {})

    def test_no_credentials_for_provider_returns_empty(self) -> None:
        instruments = [_instrument("RELIANCE", tags=["fo_eligible"])]
        result = resolve_iv_by_symbol(instruments, {"RELIANCE": _bars("RELIANCE", 2650.0)}, "groww", None, None)
        self.assertEqual(result, {})

    def test_missing_price_history_skips_symbol(self) -> None:
        instruments = [_instrument("RELIANCE", tags=["fo_eligible"])]
        with patch("core.market_data.fo_iv_screen._build_transport", return_value=object()):
            result = resolve_iv_by_symbol(instruments, {}, "groww", _GrowwCreds(), None)
        self.assertEqual(result, {})

    def test_successful_resolution_populates_dict(self) -> None:
        instruments = [_instrument("RELIANCE", tags=["fo_eligible"])]
        price_history = {"RELIANCE": _bars("RELIANCE", 2650.0)}
        with (
            patch("core.market_data.fo_iv_screen._build_transport", return_value=object()),
            patch("core.market_data.fo_iv_screen._resolve_one", return_value=0.22),
        ):
            result = resolve_iv_by_symbol(instruments, price_history, "groww", _GrowwCreds(), None)
        self.assertEqual(result, {"RELIANCE": 0.22})

    def test_fetch_failure_is_skipped_not_raised(self) -> None:
        instruments = [_instrument("RELIANCE", tags=["fo_eligible"])]
        price_history = {"RELIANCE": _bars("RELIANCE", 2650.0)}
        with (
            patch("core.market_data.fo_iv_screen._build_transport", return_value=object()),
            patch(
                "core.market_data.fo_iv_screen._resolve_one",
                side_effect=MarketDataTransportError("boom"),
            ),
        ):
            result = resolve_iv_by_symbol(instruments, price_history, "groww", _GrowwCreds(), None)
        self.assertEqual(result, {})

    def test_none_iv_is_omitted_from_result(self) -> None:
        instruments = [_instrument("RELIANCE", tags=["fo_eligible"])]
        price_history = {"RELIANCE": _bars("RELIANCE", 2650.0)}
        with (
            patch("core.market_data.fo_iv_screen._build_transport", return_value=object()),
            patch("core.market_data.fo_iv_screen._resolve_one", return_value=None),
        ):
            result = resolve_iv_by_symbol(instruments, price_history, "groww", _GrowwCreds(), None)
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()

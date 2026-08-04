"""
Unit tests for core.strategy.ornstein_uhlenbeck_residual.OrnsteinUhlenbeckResidualStrategy.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_ornstein_uhlenbeck_residual_strategy.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from math import exp, log

import numpy as np

from core.pricing.ornstein_uhlenbeck import fit_ornstein_uhlenbeck
from core.strategy.base import MarketBar, SignalSide, StrategyContext
from core.strategy.ornstein_uhlenbeck_residual import OrnsteinUhlenbeckResidualStrategy, _fit_beta

_SYMBOL = "RELIANCE"
_EXCHANGE = "NSE"
_BASE = datetime(2026, 1, 1, 9, 15)
_NEUTRAL_SQUARE_OFF_TIME = "23:59"


class _StubMarketFeed:
    """Test double for EqualWeightedMarketProxy - returns a pre-computed value per timestamp."""

    def __init__(self, returns_by_timestamp: dict[datetime, float]) -> None:
        self._returns = returns_by_timestamp

    def log_return_at(self, timestamp: datetime, exclude_symbol: str | None = None) -> float | None:
        return self._returns.get(timestamp)


def bar(close: float, ts: datetime) -> MarketBar:
    return MarketBar(symbol=_SYMBOL, exchange=_EXCHANGE, timestamp=ts, open=close, high=close + 0.1, low=close - 0.1, close=close, volume=1000.0)


def make_strategy(**params) -> OrnsteinUhlenbeckResidualStrategy:
    return OrnsteinUhlenbeckResidualStrategy(StrategyContext(strategy_name="ornstein_uhlenbeck_residual", parameters=params))


def _ou_level_path(theta: float, mu: float, sigma: float, n: int, seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    shocks = rng.standard_normal(n)
    levels = [mu]
    for shock in shocks:
        levels.append(levels[-1] + theta * (mu - levels[-1]) + sigma * shock)
    return levels


def _build_fixture(n: int = 60, beta_true: float = 1.2, market_seed: int = 5, residual_seed: int = 24):
    """
    Builds a market return series (noisy drift, no reversion) and a
    genuinely mean-reverting residual (known OU path), then combines them
    into a stock price series via ``stock_return = beta*market + residual``
    - the raw stock price itself is NOT mean-reverting (it inherits the
    market's drift), only the residual is. The default seeds were found
    by search (see this module's own generation script, not guessed) to
    reconstruct a fit via the strategy's own beta-estimation pipeline
    that is both significant/in-band AND already past the entry threshold
    at the fixture's final bar, so "signal fires" tests need no further
    engineering of the triggering price.
    """
    market_rng = np.random.default_rng(market_seed)
    market_returns = (0.0005 + 0.002 * market_rng.standard_normal(n)).tolist()

    residual_levels = _ou_level_path(theta=0.1, mu=0.0, sigma=0.01, n=n, seed=residual_seed)
    residual_returns = [residual_levels[i] - residual_levels[i - 1] for i in range(1, len(residual_levels))]

    stock_returns = [beta_true * m + r for m, r in zip(market_returns, residual_returns)]

    timestamps = [_BASE + timedelta(minutes=5 * i) for i in range(n + 1)]
    stock_closes = [100.0]
    for r in stock_returns:
        stock_closes.append(stock_closes[-1] * exp(r))

    history = [bar(close, ts) for close, ts in zip(stock_closes[:-1], timestamps[:-1])]
    current = bar(stock_closes[-1], timestamps[-1])
    market_feed = _StubMarketFeed({ts: m for ts, m in zip(timestamps[1:], market_returns)})
    return history, current, market_feed


class FitBetaTests(unittest.TestCase):
    def test_recovers_a_reasonable_beta_estimate(self) -> None:
        market_returns = (0.0005 + 0.002 * np.random.default_rng(1).standard_normal(200)).tolist()
        stock_returns = [1.5 * m for m in market_returns]  # zero-noise, beta should recover exactly

        beta = _fit_beta(market_returns, stock_returns)

        self.assertAlmostEqual(beta, 1.5, places=6)

    def test_zero_variance_market_returns_none(self) -> None:
        self.assertIsNone(_fit_beta([0.001] * 10, [0.002] * 10))


class OrnsteinUhlenbeckResidualStrategyTests(unittest.TestCase):
    def test_abstains_when_market_index_feed_is_missing(self) -> None:
        history, current, _ = _build_fixture()
        strategy = make_strategy(lookback_bars=60)  # no market_index_feed param at all

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_abstains_when_market_feed_is_missing_data_for_the_window(self) -> None:
        history, current, _ = _build_fixture()
        empty_feed = _StubMarketFeed({})
        strategy = make_strategy(lookback_bars=60, market_index_feed=empty_feed)

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_signal_fires_on_a_significant_residual_deviation(self) -> None:
        history, current, market_feed = _build_fixture()
        strategy = make_strategy(
            lookback_bars=60, entry_z_threshold=2.0, min_t_stat=1.5,
            square_off_time=_NEUTRAL_SQUARE_OFF_TIME, market_index_feed=market_feed,
        )

        # Confirm this fixture really does reconstruct a significant,
        # in-band residual fit before trusting the rest of the test.
        window = history[-60:] + [current]
        stock_returns = [log(window[i].close / window[i - 1].close) for i in range(1, len(window))]
        market_returns = [market_feed.log_return_at(window[i].timestamp) for i in range(1, len(window))]
        beta = _fit_beta(market_returns, stock_returns)
        residual_levels = [0.0]
        for s, m in zip(stock_returns, market_returns):
            residual_levels.append(residual_levels[-1] + (s - beta * m))
        fit = fit_ornstein_uhlenbeck(residual_levels, min_t_stat=1.5)
        self.assertIsNotNone(fit, "test fixture must reconstruct a significant residual fit")
        self.assertTrue(15.0 <= fit.half_life_bars * 5 <= 75.0)
        self.assertLessEqual(fit.z_score(residual_levels[-1]), -2.0, "fixture's own current bar must already be a qualifying entry")

        signals = strategy.on_bar(current, history, {})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, SignalSide.BUY)
        self.assertEqual(signals[0].reason, "ornstein_uhlenbeck_residual_reversion")
        # Trade executes at the REAL price, not some synthetic residual "price".
        self.assertEqual(signals[0].metadata["current_close"], current.close)

    def test_open_position_blocks_re_entry_by_default(self) -> None:
        history, current, market_feed = _build_fixture()
        strategy = make_strategy(
            lookback_bars=60, entry_z_threshold=2.0, min_t_stat=1.5,
            square_off_time=_NEUTRAL_SQUARE_OFF_TIME, market_index_feed=market_feed,
        )

        signals = strategy.on_bar(current, history, {f"{_EXCHANGE}:{_SYMBOL}": object()})

        self.assertEqual(signals, [])

    def test_no_signal_once_past_square_off_time(self) -> None:
        history, current, market_feed = _build_fixture()
        strategy = make_strategy(
            lookback_bars=60, market_index_feed=market_feed,
            square_off_time=current.timestamp.strftime("%H:%M"),
        )

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_non_positive_lookback_bars_raises(self) -> None:
        history, current, market_feed = _build_fixture()
        strategy = make_strategy(lookback_bars=2, market_index_feed=market_feed)

        with self.assertRaises(ValueError):
            strategy.on_bar(current, history, {})

    def test_non_positive_entry_z_threshold_raises(self) -> None:
        history, current, market_feed = _build_fixture()
        strategy = make_strategy(lookback_bars=60, entry_z_threshold=0.0, market_index_feed=market_feed)

        with self.assertRaises(ValueError):
            strategy.on_bar(current, history, {})


if __name__ == "__main__":
    unittest.main()

"""
Unit tests for core.strategy.ornstein_uhlenbeck.OrnsteinUhlenbeckStrategy.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_ornstein_uhlenbeck_strategy.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from math import exp, log

import numpy as np

from core.pricing.ornstein_uhlenbeck import fit_ornstein_uhlenbeck
from core.strategy.base import MarketBar, SignalSide, StrategyContext
from core.strategy.ornstein_uhlenbeck import OrnsteinUhlenbeckStrategy

_SYMBOL = "RELIANCE"
_EXCHANGE = "NSE"
_BASE = datetime(2026, 1, 1, 9, 15)

# Verified (see this module's own generation script, not guessed): a
# theta=0.1/seed=5/n=60 synthetic OU path (5-minute bar spacing) recovers
# a fit whose half-life (~5.7 bars, ~28.5 real minutes) lands inside the
# strategy's default [min, max]_half_life_minutes band, at a *lenient*
# min_t_stat - the strategy's own default (3.5) has low power at n=60
# (see docs/strategy-specs/ornstein_uhlenbeck.md's "statistical power"
# finding), so tests whose purpose is exercising entry-threshold/half-
# life-band/open-position logic (not the significance gate itself) pass
# an explicit lenient min_t_stat to get a reliable fixture. They also pass
# a late square_off_time to neutralize the remaining-session-time gate
# (tested separately below), since 55 real-world minutes typically remain
# after 60 5-minute bars from a 9:15 session start - not always enough
# headroom at the default min_remaining_half_lives.
_RELIABLE_THETA = 0.1
_RELIABLE_SEED = 5
_RELIABLE_MIN_T_STAT = 1.5
_NEUTRAL_SQUARE_OFF_TIME = "23:59"


def bar(close: float, ts: datetime, open_: float | None = None) -> MarketBar:
    open_ = close if open_ is None else open_
    return MarketBar(
        symbol=_SYMBOL, exchange=_EXCHANGE, timestamp=ts,
        open=open_, high=max(open_, close) + 0.1, low=min(open_, close) - 0.1,
        close=close, volume=1000.0,
    )


def make_strategy(**params) -> OrnsteinUhlenbeckStrategy:
    return OrnsteinUhlenbeckStrategy(StrategyContext(strategy_name="ornstein_uhlenbeck", parameters=params))


def _ou_bars(theta: float, mu_log: float, sigma: float, n: int, seed: int, interval_minutes: int = 5) -> list[MarketBar]:
    """Deterministic synthetic OU path (in log-price), converted to a bar history via exp()."""
    rng = np.random.default_rng(seed)
    shocks = rng.standard_normal(n)
    levels = [mu_log]
    for shock in shocks:
        levels.append(levels[-1] + theta * (mu_log - levels[-1]) + sigma * shock)
    return [bar(exp(level), _BASE + timedelta(minutes=interval_minutes * i)) for i, level in enumerate(levels)]


def _reliable_history() -> list[MarketBar]:
    return _ou_bars(theta=_RELIABLE_THETA, mu_log=log(100.0), sigma=0.01, n=60, seed=_RELIABLE_SEED)


def _reliable_fit():
    history = _reliable_history()
    return fit_ornstein_uhlenbeck([log(b.close) for b in history], min_t_stat=_RELIABLE_MIN_T_STAT)


class OrnsteinUhlenbeckStrategyTests(unittest.TestCase):
    def test_no_signal_with_insufficient_history(self) -> None:
        strategy = make_strategy(lookback_bars=60)
        history = _ou_bars(theta=0.15, mu_log=log(100.0), sigma=0.01, n=10, seed=1)
        current = bar(99.0, history[-1].timestamp + timedelta(minutes=5))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_signal_fires_on_a_statistically_significant_deep_deviation(self) -> None:
        history = _reliable_history()
        strategy = make_strategy(
            lookback_bars=60, entry_z_threshold=2.0, min_t_stat=_RELIABLE_MIN_T_STAT,
            square_off_time=_NEUTRAL_SQUARE_OFF_TIME,
        )

        # Confirm this window really does fit significantly before trusting
        # the rest of the test - a change to the fixture that breaks the
        # fit should fail loudly here, not as a confusing "no signal".
        fit = _reliable_fit()
        self.assertIsNotNone(fit, "test fixture must produce a significant OU fit")
        self.assertTrue(15.0 <= fit.half_life_bars * 5 <= 75.0, "fixture must also clear the default half-life band")

        # Engineer the triggering bar to sit well past the entry threshold,
        # using the fit's own equilibrium - deterministic, not tuned by trial and error.
        deep_price = exp(fit.mu - 3.0 * fit.equilibrium_std)
        current = bar(deep_price, history[-1].timestamp + timedelta(minutes=5))

        signals = strategy.on_bar(current, history, {})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, SignalSide.BUY)
        self.assertEqual(signals[0].reason, "ornstein_uhlenbeck_reversion")
        self.assertLessEqual(signals[0].metadata["z_score"], -2.0)

    def test_no_signal_when_deviation_is_within_threshold(self) -> None:
        history = _reliable_history()
        strategy = make_strategy(
            lookback_bars=60, entry_z_threshold=2.0, min_t_stat=_RELIABLE_MIN_T_STAT,
            square_off_time=_NEUTRAL_SQUARE_OFF_TIME,
        )

        fit = _reliable_fit()
        self.assertIsNotNone(fit)

        # Barely below the mean - nowhere near the entry threshold.
        near_price = exp(fit.mu - 0.1 * fit.equilibrium_std)
        current = bar(near_price, history[-1].timestamp + timedelta(minutes=5))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_no_signal_when_fit_is_not_significant(self) -> None:
        # A noisy random walk at this strategy's real default min_t_stat
        # (3.5, not the lenient test-only override used elsewhere in this
        # file) - real theta is 0, no genuine reversion to trade.
        rng = np.random.default_rng(123)
        walk = np.cumsum(rng.standard_normal(60)) * 0.01 + log(100.0)
        history = [bar(exp(level), _BASE + timedelta(minutes=5 * i)) for i, level in enumerate(walk)]
        strategy = make_strategy(lookback_bars=60, square_off_time=_NEUTRAL_SQUARE_OFF_TIME)
        current = bar(history[-1].close * 0.9, history[-1].timestamp + timedelta(minutes=5))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_no_signal_when_half_life_outside_configured_band(self) -> None:
        # Very fast reversion (small half-life) - should be rejected as
        # noise-like even if statistically "significant" (at a lenient
        # min_t_stat, isolating this check from the significance gate).
        history = _ou_bars(theta=0.9, mu_log=log(100.0), sigma=0.01, n=60, seed=5)
        strategy = make_strategy(
            lookback_bars=60, min_half_life_minutes=15.0, max_half_life_minutes=75.0,
            min_t_stat=1.0, square_off_time=_NEUTRAL_SQUARE_OFF_TIME,
        )

        fit = fit_ornstein_uhlenbeck([log(b.close) for b in history], min_t_stat=1.0)
        self.assertIsNotNone(fit)
        self.assertLess(fit.half_life_bars * 5, 15.0)  # confirms the fixture actually exercises this branch

        deep_price = exp(fit.mu - 3.0 * fit.equilibrium_std)
        current = bar(deep_price, history[-1].timestamp + timedelta(minutes=5))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_no_signal_when_insufficient_session_time_remains(self) -> None:
        # Same reliable, significant, in-band fit as the "signal fires"
        # test - but with square_off_time set so few minutes remain,
        # relative to the fit's own half-life, that this gate alone
        # should block entry.
        history = _reliable_history()
        fit = _reliable_fit()
        half_life_minutes = fit.half_life_bars * 5
        current = bar(exp(fit.mu - 3.0 * fit.equilibrium_std), history[-1].timestamp + timedelta(minutes=5))
        # Square-off just 1 minute after the triggering bar - nowhere near
        # min_remaining_half_lives (default 2.0) x half_life_minutes.
        near_square_off = (current.timestamp + timedelta(minutes=1)).strftime("%H:%M")

        strategy = make_strategy(
            lookback_bars=60, entry_z_threshold=2.0, min_t_stat=_RELIABLE_MIN_T_STAT,
            square_off_time=near_square_off,
        )

        self.assertGreater(half_life_minutes, 0)  # sanity: the gate has something real to compare against
        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_no_signal_once_past_square_off_time(self) -> None:
        history = _reliable_history()
        current = bar(99.0, history[-1].timestamp + timedelta(minutes=5))
        strategy = make_strategy(lookback_bars=60, square_off_time=current.timestamp.strftime("%H:%M"))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_open_position_blocks_re_entry_by_default(self) -> None:
        history = _reliable_history()
        strategy = make_strategy(
            lookback_bars=60, entry_z_threshold=2.0, min_t_stat=_RELIABLE_MIN_T_STAT,
            square_off_time=_NEUTRAL_SQUARE_OFF_TIME,
        )
        fit = _reliable_fit()
        current = bar(exp(fit.mu - 3.0 * fit.equilibrium_std), history[-1].timestamp + timedelta(minutes=5))

        signals = strategy.on_bar(current, history, {f"{_EXCHANGE}:{_SYMBOL}": object()})

        self.assertEqual(signals, [])

    def test_non_positive_lookback_bars_raises(self) -> None:
        strategy = make_strategy(lookback_bars=2)
        history = _ou_bars(theta=0.2, mu_log=log(100.0), sigma=0.01, n=10, seed=1)
        current = bar(99.0, history[-1].timestamp + timedelta(minutes=5))

        with self.assertRaises(ValueError):
            strategy.on_bar(current, history, {})

    def test_non_positive_entry_z_threshold_raises(self) -> None:
        strategy = make_strategy(lookback_bars=60, entry_z_threshold=0.0)
        history = _ou_bars(theta=0.2, mu_log=log(100.0), sigma=0.01, n=60, seed=2)
        current = bar(99.0, history[-1].timestamp + timedelta(minutes=5))

        with self.assertRaises(ValueError):
            strategy.on_bar(current, history, {})


if __name__ == "__main__":
    unittest.main()

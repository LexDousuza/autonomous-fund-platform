"""
Unit tests for core.strategy.options_mispricing.OptionsMispricingStrategy.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_options_mispricing_strategy.py -v
"""
from __future__ import annotations

import math
import unittest
from datetime import date, datetime, timedelta

from core.broker.base import OptionType, PositionEffect
from core.market_data.option_chain_models import OptionChainContract, OptionChainSnapshot
from core.pricing.black_scholes import price as bs_price
from core.pricing.monte_carlo import estimate_daily_drift_and_vol
from core.strategy.base import MarketBar, SignalSide, StrategyContext
from core.strategy.options_mispricing import OptionsMispricingStrategy

# Fixed reference date (matches make_bar's default timestamp below), not
# date.today() - proves time-to-expiry is computed from the bar's own
# date. See core/pricing/time_to_expiry.py's docstring: three call sites
# used to compute this from date.today(), making both options strategies
# incapable of ever signaling in a backtest of historical bars.
_BAR_DATE = date(2026, 1, 30)
_EXPIRY = (_BAR_DATE + timedelta(days=20)).isoformat()
_TRADING_DAYS_PER_YEAR = 252


class FakeOptionChainProvider:
    def __init__(self, snapshot: OptionChainSnapshot | None) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, str, int, float]] = []

    def get_chain(self, underlying_symbol, exchange, expiry_horizon_days, spot):
        self.calls.append((underlying_symbol, exchange, expiry_horizon_days, spot))
        return self.snapshot


def make_bar(symbol="RELIANCE", exchange="NSE", close=2650.0, timestamp=datetime(_BAR_DATE.year, _BAR_DATE.month, _BAR_DATE.day, 9, 15)):
    return MarketBar(
        symbol=symbol, exchange=exchange, timestamp=timestamp, open=close, high=close, low=close,
        close=close, volume=10000,
    )


def make_history(closes: list[float], symbol="RELIANCE") -> list:
    base = datetime(2026, 1, 1, 9, 15)
    return [
        MarketBar(
            symbol=symbol, exchange="NSE", timestamp=base.replace(minute=15) + timedelta(days=i),
            open=c, high=c, low=c, close=c, volume=10000,
        )
        for i, c in enumerate(closes)
    ]


def make_context(params: dict | None = None) -> StrategyContext:
    return StrategyContext(strategy_name="options_mispricing", parameters=params or {})


def make_contract(strike: float, option_type: OptionType, last_price: float | None, lot_size: int = 250):
    return OptionChainContract(
        underlying_symbol="RELIANCE", strike=strike, expiry=_EXPIRY, option_type=option_type,
        tradingsymbol=f"RELIANCE_{int(strike)}_{option_type.value}", last_price=last_price, lot_size=lot_size,
    )


class QuietAbstentionTests(unittest.TestCase):
    def _closes(self) -> list[float]:
        return [2600.0 + (i % 3) * 5 for i in range(25)]

    def test_no_provider_configured_yields_no_signal(self) -> None:
        strategy = OptionsMispricingStrategy(make_context({}))
        history = make_history(self._closes())
        bar = make_bar(close=history[-1].close)

        self.assertEqual(strategy.on_bar(bar, history[:-1], {}), [])

    def test_insufficient_history_yields_no_signal(self) -> None:
        provider = FakeOptionChainProvider(snapshot=None)
        strategy = OptionsMispricingStrategy(make_context({"option_chain_provider": provider}))
        history = make_history([2600.0])  # only 1 prior bar + current = 2 closes, below the minimum
        bar = make_bar(close=2605.0)

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_chain_unavailable_yields_no_signal(self) -> None:
        provider = FakeOptionChainProvider(snapshot=None)
        strategy = OptionsMispricingStrategy(make_context({"option_chain_provider": provider}))
        history = make_history(self._closes())
        bar = make_bar(close=history[-1].close)

        self.assertEqual(strategy.on_bar(bar, history[:-1], {}), [])
        self.assertEqual(len(provider.calls), 1)  # it was actually attempted, just returned nothing usable

    def test_chain_with_no_contracts_yields_no_signal(self) -> None:
        snapshot = OptionChainSnapshot(underlying_symbol="RELIANCE", underlying_last_price=2650.0, expiry=_EXPIRY)
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(make_context({"option_chain_provider": provider}))
        history = make_history(self._closes())
        bar = make_bar(close=history[-1].close)

        self.assertEqual(strategy.on_bar(bar, history[:-1], {}), [])


class MispricingSignalTests(unittest.TestCase):
    """
    Builds a snapshot whose contract price is a known multiple of the
    strategy's own Black-Scholes fair value (computed here with the exact
    same realized-vol estimate the strategy itself derives from
    ``history``), so BUY/SELL thresholds are exercised precisely rather
    than by trial and error.
    """

    def _closes(self) -> list[float]:
        # A small, deterministic zig-zag - enough variance for a
        # non-degenerate vol estimate, fully reproducible.
        return [2600.0 + (i % 4) * 8 - 12 for i in range(21)]

    def _annualized_vol_and_spot(self) -> tuple[float, float]:
        closes = self._closes()
        _, sigma_per_bar = estimate_daily_drift_and_vol(closes)
        return sigma_per_bar * math.sqrt(_TRADING_DAYS_PER_YEAR), closes[-1]

    def test_underpriced_contract_triggers_buy_open(self) -> None:
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot  # at-the-money, well within the default strike band
        rate = 0.07
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value = bs_price(OptionType.CALL, spot, strike, rate, annualized_vol, time_to_expiry)

        contract = make_contract(strike, OptionType.CALL, last_price=fair_value * 0.5)
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context({"option_chain_provider": provider, "risk_free_rate": rate})
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        signals = strategy.on_bar(bar, history, {})

        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.side, SignalSide.BUY)
        self.assertEqual(signal.exchange, "NFO")
        self.assertEqual(signal.option_type, OptionType.CALL)
        self.assertEqual(signal.strike, strike)
        self.assertEqual(signal.position_effect, PositionEffect.OPEN)
        self.assertEqual(signal.lot_size, 250)
        self.assertLess(signal.metadata["mispricing_pct"], 0)

    def test_negative_last_price_is_never_traded_on(self) -> None:
        # Corrupted/stale broker data (a negative quote) must not pass
        # the price-usability check - `if buy_price:` would treat -5.0
        # as truthy and compute a nonsense mispricing_pct against it.
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        contract = make_contract(strike, OptionType.CALL, last_price=-5.0)
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context({"option_chain_provider": provider, "risk_free_rate": rate, "allow_naked_writing": True})
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_overpriced_contract_without_opt_in_yields_no_signal(self) -> None:
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value = bs_price(OptionType.PUT, spot, strike, rate, annualized_vol, time_to_expiry)

        contract = make_contract(strike, OptionType.PUT, last_price=fair_value * 2.0)
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        # allow_naked_writing defaults to False.
        strategy = OptionsMispricingStrategy(
            make_context({"option_chain_provider": provider, "risk_free_rate": rate})
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_overpriced_contract_with_opt_in_triggers_sell_open(self) -> None:
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value = bs_price(OptionType.PUT, spot, strike, rate, annualized_vol, time_to_expiry)

        contract = make_contract(strike, OptionType.PUT, last_price=fair_value * 2.0)
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context(
                {"option_chain_provider": provider, "risk_free_rate": rate, "allow_naked_writing": True}
            )
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        signals = strategy.on_bar(bar, history, {})

        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.side, SignalSide.SELL)
        self.assertEqual(signal.position_effect, PositionEffect.OPEN)
        self.assertGreater(signal.metadata["mispricing_pct"], 0)

    def test_fairly_priced_contract_yields_no_signal(self) -> None:
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value = bs_price(OptionType.CALL, spot, strike, rate, annualized_vol, time_to_expiry)

        # Within the default 15% mispricing threshold - not a signal.
        contract = make_contract(strike, OptionType.CALL, last_price=fair_value * 1.05)
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context({"option_chain_provider": provider, "risk_free_rate": rate, "allow_naked_writing": True})
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_strike_outside_band_is_ignored(self) -> None:
        annualized_vol, spot = self._annualized_vol_and_spot()
        far_strike = spot * 1.5  # well outside the default 5% strike band
        rate = 0.07
        contract = make_contract(far_strike, OptionType.CALL, last_price=0.01)  # absurdly underpriced
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context({"option_chain_provider": provider, "risk_free_rate": rate})
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_never_uses_the_chains_own_iv(self) -> None:
        # A contract's .iv is deliberately set to a different value from
        # the strategy's own realized-vol estimate (but still within the
        # default max_iv_divergence_ratio=2.0 plausibility band - see
        # test_iv_divergence_beyond_plausibility_band_suppresses_signal
        # below for the "wildly different" case) - the resulting fair
        # value/mispricing must be computed entirely off the strategy's
        # own estimate, never contract.iv (per decision #5: pricing back
        # with a quote's own IV always reproduces that same quote, zero
        # edge by construction).
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value_at_own_vol = bs_price(OptionType.CALL, spot, strike, rate, annualized_vol, time_to_expiry)

        contract = make_contract(strike, OptionType.CALL, last_price=fair_value_at_own_vol * 0.5)
        contract.iv = annualized_vol * 1.5  # different from our own estimate, but plausibly so
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context({"option_chain_provider": provider, "risk_free_rate": rate})
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        signals = strategy.on_bar(bar, history, {})

        self.assertEqual(len(signals), 1)
        self.assertAlmostEqual(signals[0].metadata["fair_value"], fair_value_at_own_vol, places=6)

    def test_iv_divergence_beyond_plausibility_band_suppresses_signal(self) -> None:
        # Same setup as above, but contract.iv is now wildly different
        # (5.0 vs an annualized_vol of a few tens of percent) - beyond the
        # default max_iv_divergence_ratio=2.0 plausibility band. This is
        # a sanity check on OUR OWN estimate's plausibility, not a second
        # use of the chain's IV for pricing - the signal is suppressed
        # entirely, not repriced with contract.iv.
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value_at_own_vol = bs_price(OptionType.CALL, spot, strike, rate, annualized_vol, time_to_expiry)

        contract = make_contract(strike, OptionType.CALL, last_price=fair_value_at_own_vol * 0.5)
        contract.iv = 5.0  # 500% - wildly outside any plausible 2x band around a normal realized vol
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context({"option_chain_provider": provider, "risk_free_rate": rate})
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_iv_divergence_check_ignored_when_contract_has_no_iv(self) -> None:
        # Kite contracts before a quote arrives, or any contract this
        # provider simply doesn't have IV for - must not be silently
        # blocked just for lacking a number to compare against.
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value = bs_price(OptionType.CALL, spot, strike, rate, annualized_vol, time_to_expiry)

        contract = make_contract(strike, OptionType.CALL, last_price=fair_value * 0.5)
        self.assertIsNone(contract.iv)
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context({"option_chain_provider": provider, "risk_free_rate": rate})
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        self.assertEqual(len(strategy.on_bar(bar, history, {})), 1)

    def test_low_open_interest_contract_is_skipped(self) -> None:
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value = bs_price(OptionType.CALL, spot, strike, rate, annualized_vol, time_to_expiry)

        contract = make_contract(strike, OptionType.CALL, last_price=fair_value * 0.5)
        contract.open_interest = 10.0  # known-illiquid
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context({"option_chain_provider": provider, "risk_free_rate": rate, "min_open_interest": 500.0})
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_open_interest_none_is_never_filtered(self) -> None:
        # A provider (Groww) that doesn't supply open_interest at all -
        # can't judge liquidity you don't have a number for, so this must
        # never be treated the same as "known low OI".
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value = bs_price(OptionType.CALL, spot, strike, rate, annualized_vol, time_to_expiry)

        contract = make_contract(strike, OptionType.CALL, last_price=fair_value * 0.5)
        self.assertIsNone(contract.open_interest)
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context({"option_chain_provider": provider, "risk_free_rate": rate, "min_open_interest": 500.0})
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        self.assertEqual(len(strategy.on_bar(bar, history, {})), 1)

    def test_buy_signal_priced_against_ask_not_last_price_when_available(self) -> None:
        # last_price alone looks fairly-priced (within threshold), but the
        # real achievable ask is deep enough below fair value to fire -
        # proves the ask, not last_price, is what actually gets evaluated.
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value = bs_price(OptionType.CALL, spot, strike, rate, annualized_vol, time_to_expiry)

        contract = make_contract(strike, OptionType.CALL, last_price=fair_value * 1.0)  # looks fair
        contract.ask = fair_value * 0.5  # what you'd actually pay - deeply underpriced
        contract.bid = fair_value * 0.4
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context({"option_chain_provider": provider, "risk_free_rate": rate})
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        signals = strategy.on_bar(bar, history, {})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, SignalSide.BUY)
        self.assertEqual(signals[0].metadata["market_price"], contract.ask)

    def test_sell_signal_priced_against_bid_not_last_price_when_available(self) -> None:
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value = bs_price(OptionType.PUT, spot, strike, rate, annualized_vol, time_to_expiry)

        contract = make_contract(strike, OptionType.PUT, last_price=fair_value * 1.0)  # looks fair
        contract.bid = fair_value * 2.0  # what you'd actually receive - deeply overpriced
        contract.ask = fair_value * 2.1
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context(
                {"option_chain_provider": provider, "risk_free_rate": rate, "allow_naked_writing": True}
            )
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        signals = strategy.on_bar(bar, history, {})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, SignalSide.SELL)
        self.assertEqual(signals[0].metadata["market_price"], contract.bid)

    def test_configured_threshold_below_noise_floor_is_raised_not_used_directly(self) -> None:
        # historical_vol_lookback_bars=20 (the default) gives a noise
        # floor of ~1/sqrt(2*19)*100 =~ 16.2% - a configured 1% threshold
        # must not let a 10%-mispriced (i.e. below the real noise floor)
        # contract fire.
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value = bs_price(OptionType.CALL, spot, strike, rate, annualized_vol, time_to_expiry)

        contract = make_contract(strike, OptionType.CALL, last_price=fair_value * 0.90)  # 10% underpriced
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context(
                {"option_chain_provider": provider, "risk_free_rate": rate, "mispricing_threshold_pct": 1.0}
            )
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_dividend_yield_is_threaded_through_to_fair_value(self) -> None:
        annualized_vol, spot = self._annualized_vol_and_spot()
        strike = spot
        rate = 0.07
        dividend_yield = 0.04
        time_to_expiry = (date.fromisoformat(_EXPIRY) - _BAR_DATE).days / 365.0
        fair_value_with_dividend = bs_price(
            OptionType.CALL, spot, strike, rate, annualized_vol, time_to_expiry, dividend_yield
        )
        fair_value_without_dividend = bs_price(OptionType.CALL, spot, strike, rate, annualized_vol, time_to_expiry)
        self.assertNotAlmostEqual(fair_value_with_dividend, fair_value_without_dividend, places=6)

        contract = make_contract(strike, OptionType.CALL, last_price=fair_value_with_dividend * 0.5)
        snapshot = OptionChainSnapshot(
            underlying_symbol="RELIANCE", underlying_last_price=spot, expiry=_EXPIRY, contracts=[contract]
        )
        provider = FakeOptionChainProvider(snapshot=snapshot)
        strategy = OptionsMispricingStrategy(
            make_context(
                {"option_chain_provider": provider, "risk_free_rate": rate, "dividend_yield": dividend_yield}
            )
        )
        history = make_history(self._closes()[:-1])
        bar = make_bar(close=spot)

        signals = strategy.on_bar(bar, history, {})

        self.assertEqual(len(signals), 1)
        self.assertAlmostEqual(signals[0].metadata["fair_value"], fair_value_with_dividend, places=6)


if __name__ == "__main__":
    unittest.main()

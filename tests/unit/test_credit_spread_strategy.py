"""
Unit tests for core.strategy.credit_spread.VerticalCreditSpreadStrategy.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_credit_spread_strategy.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import unittest

from core.broker.base import OptionType, PositionEffect
from core.market_data.option_chain_models import OptionChainContract, OptionChainSnapshot
from core.pricing.black_scholes import greeks as compute_greeks
from core.strategy.base import MarketBar, SignalSide, StrategyContext
from core.strategy.credit_spread import VerticalCreditSpreadStrategy

# Fixed reference date, deliberately far from wall-clock "today" - proves
# time-to-expiry is computed from the bar's own date, not date.today()
# (see core/pricing/time_to_expiry.py's docstring for the bug this guards
# against: three call sites used to compute this from date.today(), which
# made both options strategies incapable of ever signaling in a backtest
# of historical bars).
_REFERENCE_DATE = date(2020, 1, 1)
_EXPIRY = (_REFERENCE_DATE + timedelta(days=7)).isoformat()
_SPOT = 100.0
_IV = 0.24
_RATE = 0.07
_TIME_TO_EXPIRY = 7 / 365.0


class FakeOptionChainProvider:
    def __init__(self, snapshot: OptionChainSnapshot | None) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple] = []

    def get_chain(self, underlying_symbol, exchange, expiry_horizon_days, spot):
        self.calls.append((underlying_symbol, exchange, expiry_horizon_days, spot))
        return self.snapshot


def _contract(
    strike: float,
    option_type: OptionType,
    iv: float = _IV,
    open_interest: float | None = 100000.0,
    spread: float = 0.0,
) -> OptionChainContract:
    fair = _bs_price(option_type, strike, iv)
    return OptionChainContract(
        underlying_symbol="NIFTY",
        strike=strike,
        expiry=_EXPIRY,
        option_type=option_type,
        tradingsymbol=f"NIFTY_{option_type.value}_{int(strike)}",
        last_price=fair,
        iv=iv,
        lot_size=50,
        bid=fair - spread,
        ask=fair + spread,
        open_interest=open_interest,
    )


def _bs_price(option_type: OptionType, strike: float, iv: float) -> float:
    from core.pricing.black_scholes import price as bs_price

    return bs_price(option_type, _SPOT, strike, _RATE, iv, _TIME_TO_EXPIRY)


def _delta(option_type: OptionType, strike: float, iv: float = _IV) -> float:
    return compute_greeks(option_type, _SPOT, strike, _RATE, iv, _TIME_TO_EXPIRY).delta


def _make_snapshot(strikes: list[float], option_type: OptionType, **kwargs) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        underlying_symbol="NIFTY",
        underlying_last_price=_SPOT,
        expiry=_EXPIRY,
        contracts=[_contract(s, option_type, **kwargs) for s in strikes],
    )


def _flat_history(count: int, base_time: datetime | None = None) -> list[MarketBar]:
    """Near-flat closes (tiny deterministic wiggle) -> a realized vol
    small enough that atm_iv/realized_vol clears the default 1.2
    threshold without needing to hand-compute the exact figure."""
    base_time = base_time or datetime(_REFERENCE_DATE.year, _REFERENCE_DATE.month, _REFERENCE_DATE.day, 9, 15, 0)
    bars = []
    for i in range(count):
        wiggle = 0.01 if i % 2 == 0 else -0.01
        bars.append(
            MarketBar(
                symbol="NIFTY",
                exchange="NFO",
                timestamp=base_time + timedelta(minutes=i),
                open=_SPOT,
                high=_SPOT + 0.02,
                low=_SPOT - 0.02,
                close=_SPOT + wiggle,
                volume=1000.0,
            )
        )
    return bars


def _current_bar(history: list[MarketBar]) -> MarketBar:
    last = history[-1]
    return MarketBar(
        symbol="NIFTY",
        exchange="NFO",
        timestamp=last.timestamp + timedelta(minutes=1),
        open=_SPOT,
        high=_SPOT,
        low=_SPOT,
        close=_SPOT,
        volume=1000.0,
    )


def make_strategy(params: dict) -> VerticalCreditSpreadStrategy:
    return VerticalCreditSpreadStrategy(
        StrategyContext(strategy_name="vertical_credit_spread", parameters=params)
    )


DEFAULT_PARAMS = {
    "option_type": "PUT",
    "expiry_horizon_days": 7,
    "short_leg_target_delta": 0.25,
    "wing_offset_strikes": 2,
    "iv_rv_ratio_threshold": 1.2,
    "min_net_credit_pct_of_width": 0.0,  # disabled by default; dedicated test enables it
    "min_open_interest": 0.0,
    "allow_naked_writing": True,
}


def make_params(**overrides) -> dict:
    return {**DEFAULT_PARAMS, **overrides}


PUT_STRIKES = [80.0, 85.0, 90.0, 95.0, 100.0]


class QuietAbstentionTests(unittest.TestCase):
    def test_no_provider_configured_yields_no_signal(self) -> None:
        strategy = make_strategy(make_params())  # no option_chain_provider key at all
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        self.assertEqual(signals, [])

    def test_naked_writing_not_allowed_abstains_entirely(self) -> None:
        provider = FakeOptionChainProvider(_make_snapshot(PUT_STRIKES, OptionType.PUT))
        strategy = make_strategy(
            make_params(allow_naked_writing=False, option_chain_provider=provider)
        )
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        self.assertEqual(signals, [])
        provider_untouched = provider.calls == []  # abstains before ever fetching a chain
        self.assertTrue(provider_untouched)

    def test_insufficient_history_yields_no_signal(self) -> None:
        provider = FakeOptionChainProvider(_make_snapshot(PUT_STRIKES, OptionType.PUT))
        strategy = make_strategy(make_params(option_chain_provider=provider))
        history = _flat_history(1)  # fewer than _MIN_CLOSES_FOR_VOL_ESTIMATE

        signals = strategy.on_bar(_current_bar(history), history, {})

        self.assertEqual(signals, [])

    def test_chain_unavailable_yields_no_signal(self) -> None:
        provider = FakeOptionChainProvider(None)
        strategy = make_strategy(make_params(option_chain_provider=provider))
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        self.assertEqual(signals, [])

    def test_chain_with_no_contracts_yields_no_signal(self) -> None:
        empty_snapshot = OptionChainSnapshot(
            underlying_symbol="NIFTY", underlying_last_price=_SPOT, expiry=_EXPIRY, contracts=[]
        )
        provider = FakeOptionChainProvider(empty_snapshot)
        strategy = make_strategy(make_params(option_chain_provider=provider))
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        self.assertEqual(signals, [])

    def test_iv_rv_ratio_below_threshold_yields_no_signal(self) -> None:
        # A very high realized vol (large day-to-day swings) pushes
        # atm_iv/realized_vol below the threshold - premium isn't rich.
        # Must stay on _REFERENCE_DATE (not some other date) so this test
        # actually exercises the IV/RV check, not an unrelated expired-
        # option abstention from a mismatched bar date.
        base_time = datetime(_REFERENCE_DATE.year, _REFERENCE_DATE.month, _REFERENCE_DATE.day, 9, 15, 0)
        wild_history = [
            MarketBar(
                symbol="NIFTY", exchange="NFO", timestamp=base_time + timedelta(minutes=i),
                open=_SPOT, high=_SPOT + 10, low=_SPOT - 10,
                close=_SPOT + (15 if i % 2 == 0 else -15), volume=1000.0,
            )
            for i in range(5)
        ]
        provider = FakeOptionChainProvider(_make_snapshot(PUT_STRIKES, OptionType.PUT))
        strategy = make_strategy(make_params(option_chain_provider=provider, iv_rv_ratio_threshold=1.2))

        signals = strategy.on_bar(_current_bar(wild_history), wild_history, {})

        self.assertEqual(signals, [])

    def test_abstains_when_a_leg_of_this_underlying_is_already_open(self) -> None:
        # Without this guard, multi-day holding (core.backtest.
        # multi_day_position_runner) would open a brand-new spread every
        # day the IV/RV condition holds - ~75-80% of days per the
        # measured VRP - stacking exposure unboundedly, since each new
        # day picks new strikes (new symbol keys) that never trip
        # PortfolioExposureGuardrails' per-symbol cap. Today this is
        # invisible only because square_off_time forces flat every day
        # regardless - this abstention is what makes it safe once that
        # no longer applies.
        provider = FakeOptionChainProvider(_make_snapshot(PUT_STRIKES, OptionType.PUT))
        strategy = make_strategy(make_params(option_chain_provider=provider))
        history = _flat_history(5)
        open_positions = {"NFO:NIFTY_2026-01-08_90_PE": object()}  # an already-open leg of this underlying

        signals = strategy.on_bar(_current_bar(history), history, open_positions)

        self.assertEqual(signals, [])
        self.assertEqual(provider.calls, [])  # abstains before ever fetching a chain

    def test_open_leg_of_a_different_underlying_does_not_block_a_new_spread(self) -> None:
        # A NIFTY spread already open must not block a fresh BANKNIFTY
        # spread - the two indices are independent bets, not exclusive.
        provider = FakeOptionChainProvider(_make_snapshot(PUT_STRIKES, OptionType.PUT))
        strategy = make_strategy(make_params(option_chain_provider=provider))
        history = _flat_history(5)
        open_positions = {"NFO:BANKNIFTY_2026-01-08_45000_PE": object()}
        bar = _current_bar(history)  # bar.symbol == "NIFTY"

        signals = strategy.on_bar(bar, history, open_positions)

        self.assertEqual(len(signals), 2)

    def test_no_strike_far_enough_out_for_the_wing_yields_no_signal(self) -> None:
        # Only 2 strikes on this side - whichever is picked as the short
        # leg, wing_offset_strikes=2 always runs off the end of the list.
        provider = FakeOptionChainProvider(_make_snapshot([95.0, 100.0], OptionType.PUT))
        strategy = make_strategy(make_params(option_chain_provider=provider, wing_offset_strikes=2))
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        self.assertEqual(signals, [])


class SpreadConstructionTests(unittest.TestCase):
    def test_emits_exactly_two_signals_long_then_short(self) -> None:
        provider = FakeOptionChainProvider(_make_snapshot(PUT_STRIKES, OptionType.PUT))
        strategy = make_strategy(make_params(option_chain_provider=provider))
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        self.assertEqual(len(signals), 2)
        self.assertEqual(signals[0].side, SignalSide.BUY)
        self.assertFalse(signals[0].requires_prior_fill)
        self.assertEqual(signals[1].side, SignalSide.SELL)
        self.assertTrue(signals[1].requires_prior_fill)

    def test_short_leg_strike_is_nearest_to_target_delta(self) -> None:
        provider = FakeOptionChainProvider(_make_snapshot(PUT_STRIKES, OptionType.PUT))
        strategy = make_strategy(make_params(option_chain_provider=provider, short_leg_target_delta=0.25))
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        short_signal = signals[1]
        expected_strike = min(PUT_STRIKES, key=lambda s: abs(abs(_delta(OptionType.PUT, s)) - 0.25))
        self.assertEqual(short_signal.strike, expected_strike)

    def test_long_leg_is_wing_offset_strikes_further_otm_for_puts(self) -> None:
        # For puts, further OTM = lower strike = earlier in the ascending list.
        provider = FakeOptionChainProvider(_make_snapshot(PUT_STRIKES, OptionType.PUT))
        strategy = make_strategy(make_params(option_chain_provider=provider, wing_offset_strikes=2))
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        long_signal, short_signal = signals
        short_index = PUT_STRIKES.index(short_signal.strike)
        expected_long_strike = PUT_STRIKES[short_index - 2]
        self.assertEqual(long_signal.strike, expected_long_strike)
        self.assertLess(long_signal.strike, short_signal.strike)

    def test_long_leg_is_wing_offset_strikes_further_otm_for_calls(self) -> None:
        # For calls, further OTM = higher strike = later in the ascending list.
        call_strikes = [100.0, 105.0, 110.0, 115.0, 120.0]
        provider = FakeOptionChainProvider(_make_snapshot(call_strikes, OptionType.CALL))
        strategy = make_strategy(
            make_params(option_type="CALL", option_chain_provider=provider, wing_offset_strikes=2)
        )
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        long_signal, short_signal = signals
        short_index = call_strikes.index(short_signal.strike)
        expected_long_strike = call_strikes[short_index + 2]
        self.assertEqual(long_signal.strike, expected_long_strike)
        self.assertGreater(long_signal.strike, short_signal.strike)

    def test_both_legs_carry_option_fields_and_open_position_effect(self) -> None:
        provider = FakeOptionChainProvider(_make_snapshot(PUT_STRIKES, OptionType.PUT))
        strategy = make_strategy(make_params(option_chain_provider=provider))
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        for signal in signals:
            self.assertEqual(signal.exchange, "NFO")
            self.assertEqual(signal.option_type, OptionType.PUT)
            self.assertEqual(signal.expiry, _EXPIRY)
            self.assertEqual(signal.lot_size, 50)
            self.assertEqual(signal.position_effect, PositionEffect.OPEN)
            self.assertIn("market_price", signal.metadata)
            self.assertGreater(signal.metadata["market_price"], 0)

    def test_long_leg_prices_off_ask_short_leg_off_bid(self) -> None:
        # A small spread, detectable against fair value but not large
        # enough to swamp the real net-credit economics between these
        # (short-dated, mostly-OTM) strikes' own fair values.
        provider = FakeOptionChainProvider(_make_snapshot(PUT_STRIKES, OptionType.PUT, spread=0.02))
        strategy = make_strategy(make_params(option_chain_provider=provider))
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})
        long_signal, short_signal = signals

        contracts_by_strike = {c.strike: c for c in provider.snapshot.contracts}
        self.assertAlmostEqual(
            long_signal.metadata["market_price"], contracts_by_strike[long_signal.strike].ask
        )
        self.assertAlmostEqual(
            short_signal.metadata["market_price"], contracts_by_strike[short_signal.strike].bid
        )

    def test_net_credit_below_threshold_yields_no_signal(self) -> None:
        provider = FakeOptionChainProvider(_make_snapshot(PUT_STRIKES, OptionType.PUT))
        strategy = make_strategy(
            make_params(option_chain_provider=provider, min_net_credit_pct_of_width=99999.0)
        )
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        self.assertEqual(signals, [])

    def test_illiquid_short_candidate_is_skipped_in_favor_of_next_best(self) -> None:
        # Make the strike nearest the target delta illiquid; the
        # strategy must fall back to a liquid alternative, not abstain.
        target_strike = min(PUT_STRIKES, key=lambda s: abs(abs(_delta(OptionType.PUT, s)) - 0.25))
        snapshot = _make_snapshot(PUT_STRIKES, OptionType.PUT)
        for contract in snapshot.contracts:
            if contract.strike == target_strike:
                contract.open_interest = 0.0  # below the floor
        provider = FakeOptionChainProvider(snapshot)
        strategy = make_strategy(make_params(option_chain_provider=provider, min_open_interest=100.0))
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        self.assertEqual(len(signals), 2)
        self.assertNotEqual(signals[1].strike, target_strike)

    def test_illiquid_wing_yields_no_signal(self) -> None:
        provider_snapshot = _make_snapshot(PUT_STRIKES, OptionType.PUT)
        strategy = make_strategy(
            make_params(
                option_chain_provider=FakeOptionChainProvider(provider_snapshot),
                min_open_interest=100.0,
                wing_offset_strikes=2,
            )
        )
        history = _flat_history(5)
        expected_short_strike = min(PUT_STRIKES, key=lambda s: abs(abs(_delta(OptionType.PUT, s)) - 0.25))
        short_index = PUT_STRIKES.index(expected_short_strike)
        wing_strike = PUT_STRIKES[short_index - 2]
        for contract in provider_snapshot.contracts:
            if contract.strike == wing_strike:
                contract.open_interest = 0.0

        signals = strategy.on_bar(_current_bar(history), history, {})

        self.assertEqual(signals, [])

    def test_short_leg_uses_each_contracts_own_iv_not_flat_atm_iv(self) -> None:
        # Skewed chain: every contract's own IV differs from atm_iv (0.24).
        # The nearest-to-0.25-delta strike computed off each contract's OWN
        # iv differs from the strike you'd pick using flat atm_iv for all -
        # proves the fix actually reads OptionChainContract.iv, not just
        # that it doesn't crash.
        # Deltas on far-OTM, 7-day puts are extremely IV-sensitive - a
        # large swing on just two strikes is what it takes to flip which
        # one lands nearest 0.25 delta between "computed off each
        # contract's own iv" and "computed off one flat atm_iv (0.24)".
        skewed_ivs = {80.0: 0.24, 85.0: 0.24, 90.0: 1.2, 95.0: 0.10, 100.0: 0.24}
        snapshot = _make_snapshot(PUT_STRIKES, OptionType.PUT)
        for contract in snapshot.contracts:
            contract.iv = skewed_ivs[contract.strike]
        provider = FakeOptionChainProvider(snapshot)
        strategy = make_strategy(make_params(option_chain_provider=provider, short_leg_target_delta=0.25))
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        short_signal = signals[1]
        expected_with_own_iv = min(
            PUT_STRIKES, key=lambda s: abs(abs(_delta(OptionType.PUT, s, iv=skewed_ivs[s])) - 0.25)
        )
        expected_with_flat_atm_iv = min(PUT_STRIKES, key=lambda s: abs(abs(_delta(OptionType.PUT, s)) - 0.25))
        self.assertEqual(short_signal.strike, expected_with_own_iv)
        self.assertNotEqual(expected_with_own_iv, expected_with_flat_atm_iv)

    def test_short_leg_falls_back_to_atm_iv_when_contract_iv_missing(self) -> None:
        snapshot = _make_snapshot(PUT_STRIKES, OptionType.PUT)
        for contract in snapshot.contracts:
            contract.iv = None  # e.g. Kite chain with no derived IV yet
        provider = FakeOptionChainProvider(snapshot)
        strategy = make_strategy(make_params(option_chain_provider=provider, short_leg_target_delta=0.25))
        history = _flat_history(5)

        signals = strategy.on_bar(_current_bar(history), history, {})

        short_signal = signals[1]
        expected_strike = min(PUT_STRIKES, key=lambda s: abs(abs(_delta(OptionType.PUT, s)) - 0.25))
        self.assertEqual(short_signal.strike, expected_strike)

    def test_provider_is_called_with_expiry_horizon_and_spot(self) -> None:
        provider = FakeOptionChainProvider(_make_snapshot(PUT_STRIKES, OptionType.PUT))
        strategy = make_strategy(make_params(option_chain_provider=provider, expiry_horizon_days=7))
        history = _flat_history(5)
        bar = _current_bar(history)

        strategy.on_bar(bar, history, {})

        self.assertEqual(provider.calls, [("NIFTY", "NFO", 7, _SPOT)])


if __name__ == "__main__":
    unittest.main()

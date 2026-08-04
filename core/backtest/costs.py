"""
Real-world transaction costs for one closed backtest trade: brokerage,
STT, and exchange/other charges. None of this existed before - a
backtest's realized PnL was previously gross, as if every fill were free
(see core/backtest/report.py's module docstring).

Slippage is handled separately, at the fill-price level (see
core/broker/paper.py's ``slippage_bps``) - it changes what price a trade
is recorded at, whereas the costs here are separate cash outflows on top
of whatever price was actually filled.
"""

from __future__ import annotations

from core.broker.groww_fees import estimate_groww_brokerage

# Standard NSE intraday-equity STT: 0.025% of the SELL leg's value only,
# not the buy leg - a stable rate, unchanged for years.
_STT_INTRADAY_SELL_RATE = 0.00025

# Bundles NSE's exchange transaction charges (~0.00297%), SEBI turnover
# fees (~0.0001%), and stamp duty (~0.003%, buy-side only in reality) into
# one approximate combined rate applied to both legs - each component is
# small enough (sub-0.01%) that itemizing them separately wouldn't
# meaningfully change a backtest's results, only its complexity.
_EXCHANGE_AND_OTHER_CHARGES_RATE = 0.00007


def compute_round_trip_costs(entry_price: float, exit_price: float, quantity: int) -> float:
    """
    Total cost (brokerage + STT + exchange/other charges), in rupees, for
    one closed round-trip trade.

    ``quantity`` is signed (see ``ClosedTrade``): positive means the
    entry was a BUY (long - the exit is the SELL leg for STT purposes);
    negative means the entry was a SELL (naked write - the entry is the
    SELL leg for STT purposes).
    """
    abs_quantity = abs(quantity)
    entry_value = abs(entry_price) * abs_quantity
    exit_value = abs(exit_price) * abs_quantity

    brokerage = estimate_groww_brokerage(entry_value) + estimate_groww_brokerage(exit_value)
    sell_leg_value = exit_value if quantity > 0 else entry_value
    stt = sell_leg_value * _STT_INTRADAY_SELL_RATE
    exchange_and_other_charges = (entry_value + exit_value) * _EXCHANGE_AND_OTHER_CHARGES_RATE

    return brokerage + stt + exchange_and_other_charges

from __future__ import annotations

_MAX_BROKERAGE = 20.0
_MIN_BROKERAGE = 5.0
_BROKERAGE_RATE = 0.001  # 0.1% of trade value


def estimate_groww_brokerage(trade_value: float) -> float:
    """
    Groww's per-order brokerage for NSE/BSE cash equity delivery and
    intraday orders: 0.1% of trade value, capped at Rs 20, floored at Rs 5.
    """
    return min(_MAX_BROKERAGE, max(_MIN_BROKERAGE, trade_value * _BROKERAGE_RATE))

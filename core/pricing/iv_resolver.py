"""
Resolve a single at-the-money implied volatility figure from an option
chain snapshot - feeds ``core.market_data.bucket_selector``'s
risk-appetite screen a forward-looking vol number (see that module's
``iv_by_symbol`` parameter), alongside its existing trailing-daily-range
measure.
"""

from __future__ import annotations

from datetime import date

from core.market_data.option_chain_models import OptionChainSnapshot
from core.pricing.black_scholes import implied_volatility
from core.pricing.time_to_expiry import time_to_expiry_years


def resolve_atm_iv(
    snapshot: OptionChainSnapshot, spot: float, risk_free_rate: float, as_of: date
) -> float | None:
    """
    Return a single IV figure for the strike nearest ``spot`` in
    ``snapshot`` - averaging CE/PE when both are present at that strike.

    Uses each contract's already-known ``.iv`` when present (Groww
    supplies it directly); for a contract missing it (e.g. an
    unresolved Kite quote), solves it from ``.last_price`` via
    Black-Scholes instead - so this works uniformly regardless of which
    broker supplied ``snapshot``. Returns ``None`` if the snapshot has
    no contracts, or none yields a usable IV.

    ``as_of`` is required, not defaulted to ``date.today()`` - a caller
    replaying historical bars (e.g. a strategy's ``on_bar``) must pass
    that bar's own date; a caller doing a genuine real-time fetch (e.g.
    ``core.market_data.fo_iv_screen``'s bucket-selection screen) passes
    ``date.today()`` explicitly instead of it being hidden in here.
    """
    if not snapshot.contracts:
        return None

    nearest_strike = min((c.strike for c in snapshot.contracts), key=lambda s: abs(s - spot))
    at_strike = [c for c in snapshot.contracts if c.strike == nearest_strike]

    ivs: list[float] = []
    for contract in at_strike:
        if contract.iv is not None:
            ivs.append(contract.iv)
            continue
        if not contract.last_price:
            continue
        time_to_expiry = time_to_expiry_years(contract.expiry, as_of=as_of)
        if time_to_expiry <= 0:
            continue
        solved = implied_volatility(
            contract.option_type, contract.last_price, spot, contract.strike, risk_free_rate, time_to_expiry
        )
        if solved is not None:
            ivs.append(solved)

    return sum(ivs) / len(ivs) if ivs else None

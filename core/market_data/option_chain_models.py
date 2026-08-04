"""
Broker-agnostic option chain shapes, reused by both Groww's and Kite's
option chain fetchers so downstream code (``core.pricing.iv_resolver``,
``core.market_data.bucket_selector``) never branches on which broker
supplied the data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.broker.base import OptionType
from core.pricing.black_scholes import Greeks


@dataclass(slots=True)
class OptionChainContract:
    """
    A single strike/option_type contract in a chain snapshot.

    ``greeks``/``iv`` are ``None`` when the broker doesn't supply them
    directly (Kite) - ``core/market_data/kite_option_chain.py`` derives
    IV (and, from it, Greeks) from ``last_price`` via Black-Scholes for
    those. Groww returns both pre-computed and they're trusted directly
    (see ``core/market_data/groww_option_chain.py``'s docstring for why).

    ``lot_size`` defaults to ``1`` - Kite's instrument master carries a
    real per-contract lot size and populates it; Groww's option-chain
    endpoint's verified response shape carries no lot-size field at all,
    so it stays at the default there (a known gap, not a guess -
    resolving it would need a separate Groww instrument-master call this
    fetcher doesn't make). Never hardcode a real lot size from this
    field alone without cross-checking it against the broker's
    instrument master first (lot sizes roll periodically - see this
    plan's decision that they're always read fresh, never hardcoded).

    ``bid``/``ask``/``open_interest``/``volume`` are the same kind of
    provider-dependent gap as ``lot_size``: Kite's ``quote()`` response
    carries real market depth (``depth.buy[0].price``/``depth.sell[0].price``)
    and ``oi``/``volume`` fields (see
    ``core/market_data/kite_option_chain.py``, verified against
    https://kite.trade/docs/connect/v3/market-quotes/) - Groww's
    option-chain endpoint's verified response shape (see
    ``core/market_data/groww_option_chain.py``) carries none of these, so
    they stay ``None`` there. Never assume these are populated; check for
    ``None`` before using any of them (``core/strategy/options_mispricing.py``
    falls back to ``last_price``-only behavior when they aren't available).
    """

    underlying_symbol: str
    strike: float
    expiry: str
    option_type: OptionType
    tradingsymbol: str
    last_price: float | None = None
    greeks: Greeks | None = None
    iv: float | None = None
    lot_size: int = 1
    bid: float | None = None
    ask: float | None = None
    open_interest: float | None = None
    volume: float | None = None


@dataclass(slots=True)
class OptionChainSnapshot:
    """All contracts for one underlying/expiry, plus the underlying's own last price."""

    underlying_symbol: str
    underlying_last_price: float
    expiry: str
    contracts: list[OptionChainContract] = field(default_factory=list)

"""
Groww option chain fetcher.

Trusts Groww's own pre-computed Greeks/IV directly - its option-chain
endpoint already returns them per strike, and reimplementing an
options-pricing library to second-guess a broker's own numbers isn't
this platform's job. Kite has no equivalent convenience call (see
``core/market_data/kite_option_chain.py``, which derives IV itself).
"""

from __future__ import annotations

from core.broker.base import OptionType
from core.market_data.errors import MarketDataTransportError
from core.market_data.groww_transport import GrowwMarketDataTransport
from core.market_data.option_chain_models import OptionChainContract, OptionChainSnapshot
from core.pricing.black_scholes import Greeks


def fetch_expiries(transport: GrowwMarketDataTransport, exchange: str, underlying_symbol: str) -> list[str]:
    """Return the available option expiry dates (``"YYYY-MM-DD"``) for ``underlying_symbol``."""
    data = transport.get_expiries(exchange, underlying_symbol)
    expiries = data.get("expiries")
    if expiries is None:
        raise MarketDataTransportError(
            f"Malformed Groww expiries response for '{underlying_symbol}': missing 'expiries'"
        )
    return list(expiries)


def fetch_option_chain(
    transport: GrowwMarketDataTransport, exchange: str, underlying_symbol: str, expiry_date: str
) -> OptionChainSnapshot:
    """Fetch and parse the full option chain for ``underlying_symbol``/``expiry_date``."""
    data = transport.get_option_chain(exchange, underlying_symbol, expiry_date)
    try:
        underlying_ltp = float(data["underlying_ltp"])
        strikes = data["strikes"]
    except KeyError as exc:
        raise MarketDataTransportError(
            f"Malformed Groww option chain response for '{underlying_symbol}': missing {exc}"
        ) from exc

    contracts = []
    for strike_str, sides in strikes.items():
        strike = float(strike_str)
        for side_key, option_type in (("CE", OptionType.CALL), ("PE", OptionType.PUT)):
            side = sides.get(side_key)
            if side is not None:
                contracts.append(_parse_contract(underlying_symbol, strike, expiry_date, option_type, side))

    return OptionChainSnapshot(
        underlying_symbol=underlying_symbol,
        underlying_last_price=underlying_ltp,
        expiry=expiry_date,
        contracts=contracts,
    )


def _parse_contract(
    underlying_symbol: str, strike: float, expiry_date: str, option_type: OptionType, side: dict
) -> OptionChainContract:
    greeks_data = side.get("greeks") or {}
    contract_greeks = (
        Greeks(
            delta=greeks_data.get("delta", 0.0),
            gamma=greeks_data.get("gamma", 0.0),
            vega=greeks_data.get("vega", 0.0),
            theta=greeks_data.get("theta", 0.0),
            rho=greeks_data.get("rho", 0.0),
        )
        if greeks_data
        else None
    )

    return OptionChainContract(
        underlying_symbol=underlying_symbol,
        strike=strike,
        expiry=expiry_date,
        option_type=option_type,
        tradingsymbol=side.get("trading_symbol", ""),
        last_price=side.get("ltp"),
        greeks=contract_greeks,
        iv=greeks_data.get("iv"),
        # bid/ask/open_interest/volume stay at their None defaults here -
        # this endpoint's verified response shape (see this module's
        # docstring and fetch_option_chain's docstring) carries none of
        # them, same known gap as lot_size above. core/strategy/
        # options_mispricing.py falls back to last_price-only spread-blind
        # behavior when they're unavailable, rather than guessing values
        # this provider never actually returns.
    )

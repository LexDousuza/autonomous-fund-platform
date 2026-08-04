"""
Kite option chain fetcher.

Kite has no single "option chain" endpoint like Groww's - this filters
Kite's own instrument master (``KiteMarketDataTransport.list_instruments``)
for the requested underlying/expiry, batch-fetches a quote per matching
contract, and derives IV itself via ``core.pricing.black_scholes``
(Kite's quote response carries no Greeks/IV at all, unlike Groww's).
"""

from __future__ import annotations

from datetime import date

from core.broker.base import OptionType
from core.market_data.kite_transport import KiteMarketDataTransport
from core.market_data.option_chain_models import OptionChainContract, OptionChainSnapshot
from core.pricing.black_scholes import greeks as compute_greeks
from core.pricing.black_scholes import implied_volatility
from core.pricing.time_to_expiry import time_to_expiry_years

_OPTION_INSTRUMENT_TYPES = ("CE", "PE")


def fetch_expiries(transport: KiteMarketDataTransport, exchange: str, underlying_symbol: str) -> list[str]:
    """Return the sorted, distinct expiry dates (``"YYYY-MM-DD"``) available for ``underlying_symbol``."""
    instruments = transport.list_instruments(exchange)
    expiries = {
        str(row["expiry"])
        for row in instruments
        if row.get("name") == underlying_symbol and row.get("instrument_type") in _OPTION_INSTRUMENT_TYPES
    }
    return sorted(expiries)


def fetch_option_chain(
    transport: KiteMarketDataTransport,
    exchange: str,
    underlying_symbol: str,
    expiry_date: str,
    underlying_last_price: float,
    risk_free_rate: float,
) -> OptionChainSnapshot:
    """
    Build an option chain snapshot for ``underlying_symbol``/``expiry_date``
    by filtering Kite's instrument master, then one batched quote call
    for every matching contract. IV is derived from each contract's
    quoted ``last_price`` via Black-Scholes (bisection) since Kite
    doesn't return it - a contract with no quote or no recoverable IV
    (e.g. a stale/zero price) keeps ``iv=None``/``greeks=None`` rather
    than a fabricated number.
    """
    matching = [
        row
        for row in transport.list_instruments(exchange)
        if row.get("name") == underlying_symbol
        and str(row.get("expiry")) == expiry_date
        and row.get("instrument_type") in _OPTION_INSTRUMENT_TYPES
    ]

    quotes = transport.get_quotes([(row["tradingsymbol"], exchange) for row in matching])
    # Genuine real-time fetch (this file only ever calls the live Kite
    # API, never replays historical bars) - date.today() is correct here,
    # made explicit rather than hidden inside a private helper.
    time_to_expiry = time_to_expiry_years(expiry_date, as_of=date.today())

    contracts = [
        _build_contract(
            row,
            exchange,
            quotes,
            underlying_symbol,
            expiry_date,
            underlying_last_price,
            risk_free_rate,
            time_to_expiry,
        )
        for row in matching
    ]

    return OptionChainSnapshot(
        underlying_symbol=underlying_symbol,
        underlying_last_price=underlying_last_price,
        expiry=expiry_date,
        contracts=contracts,
    )


def _build_contract(
    row: dict,
    exchange: str,
    quotes: dict[str, dict],
    underlying_symbol: str,
    expiry_date: str,
    underlying_last_price: float,
    risk_free_rate: float,
    time_to_expiry: float,
) -> OptionChainContract:
    tradingsymbol = row["tradingsymbol"]
    strike = float(row["strike"])
    option_type = OptionType.CALL if row["instrument_type"] == "CE" else OptionType.PUT

    quote = quotes.get(f"{exchange}:{tradingsymbol}")
    last_price = quote.get("last_price") if quote else None
    bid, ask = _extract_best_bid_ask(quote)

    iv = None
    contract_greeks = None
    if last_price and time_to_expiry > 0:
        iv = implied_volatility(
            option_type, last_price, underlying_last_price, strike, risk_free_rate, time_to_expiry
        )
        if iv is not None:
            contract_greeks = compute_greeks(
                option_type, underlying_last_price, strike, risk_free_rate, iv, time_to_expiry
            )

    return OptionChainContract(
        underlying_symbol=underlying_symbol,
        strike=strike,
        expiry=expiry_date,
        option_type=option_type,
        tradingsymbol=tradingsymbol,
        last_price=last_price,
        greeks=contract_greeks,
        iv=iv,
        lot_size=int(row.get("lot_size", 1)),
        bid=bid,
        ask=ask,
        open_interest=quote.get("oi") if quote else None,
        volume=quote.get("volume") if quote else None,
    )


def _extract_best_bid_ask(quote: dict | None) -> tuple[float | None, float | None]:
    """
    Best bid/ask off Kite's ``quote()`` market-depth field (verified
    against https://kite.trade/docs/connect/v3/market-quotes/:
    ``depth: {"buy": [{"price", "quantity", "orders"}, ...5 levels],
    "sell": [...]}``) - unlike ``last_price``/``volume``/``ohlc``
    (already exercised elsewhere in this codebase, e.g.
    ``kite_live_feed.py``), ``depth`` is used for the first time here, so
    this is defensive about a missing/empty depth book (an illiquid
    strike can legitimately have no resting orders on one or both sides)
    rather than assuming the field is always fully populated.
    """
    if not quote:
        return None, None
    depth = quote.get("depth") or {}
    buy_levels = depth.get("buy") or []
    sell_levels = depth.get("sell") or []
    bid = buy_levels[0]["price"] if buy_levels and buy_levels[0].get("price") else None
    ask = sell_levels[0]["price"] if sell_levels and sell_levels[0].get("price") else None
    return bid, ask


class KiteOptionChainProvider:
    """
    Adapts this module's ``fetch_expiries``/``fetch_option_chain`` free
    functions (which need an explicit ``transport``, an expiry already
    chosen, and no app ever actually constructs) to the
    ``option_chain_provider`` contract every options strategy expects
    (see e.g. ``core.strategy.options_mispricing.OptionsMispricingStrategy``'s
    docstring): ``get_chain(underlying_symbol, exchange,
    expiry_horizon_days, spot) -> OptionChainSnapshot | None``.

    Picks the soonest expiry within ``expiry_horizon_days`` (never the
    furthest, and never a horizon-violating one) - ``None`` when nothing
    qualifies, matching every other quiet-abstention point in the
    options-strategy chain-fetch path (no chain, no contracts, no fair
    value) rather than raising.
    """

    def __init__(self, transport: KiteMarketDataTransport, risk_free_rate: float) -> None:
        self._transport = transport
        self._risk_free_rate = risk_free_rate

    def get_chain(
        self, underlying_symbol: str, exchange: str, expiry_horizon_days: int, spot: float
    ) -> OptionChainSnapshot | None:
        expiry_date = self._nearest_expiry_within_horizon(underlying_symbol, exchange, expiry_horizon_days)
        if expiry_date is None:
            return None

        return fetch_option_chain(
            self._transport, exchange, underlying_symbol, expiry_date, spot, self._risk_free_rate
        )

    def _nearest_expiry_within_horizon(
        self, underlying_symbol: str, exchange: str, expiry_horizon_days: int
    ) -> str | None:
        today = date.today()
        for expiry_date in fetch_expiries(self._transport, exchange, underlying_symbol):
            days_to_expiry = (date.fromisoformat(expiry_date) - today).days
            if 0 <= days_to_expiry <= expiry_horizon_days:
                return expiry_date  # fetch_expiries already returns them sorted ascending
        return None

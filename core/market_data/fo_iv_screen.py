"""
Builds the ``iv_by_symbol`` input ``core.market_data.bucket_selector``'s
``select()`` accepts, for whichever of a session's candidate instruments
are tagged ``fo_eligible`` - shared by ``apps/live_trader/main.py`` and
``apps/paper_trader/main.py`` so this wiring is written once.
"""

from __future__ import annotations

import logging
from datetime import date

from core.market_data.errors import MarketDataError
from core.market_data.groww_option_chain import fetch_expiries as groww_fetch_expiries
from core.market_data.groww_option_chain import fetch_option_chain as groww_fetch_option_chain
from core.market_data.groww_transport import (
    GrowwMarketDataCredentials,
    GrowwMarketDataTransport,
    GrowwMarketDataTransportConfig,
)
from core.market_data.kite_option_chain import fetch_expiries as kite_fetch_expiries
from core.market_data.kite_option_chain import fetch_option_chain as kite_fetch_option_chain
from core.market_data.kite_transport import (
    KiteMarketDataCredentials,
    KiteMarketDataTransport,
    KiteMarketDataTransportConfig,
)
from core.market_data.universe_models import Instrument
from core.pricing.iv_resolver import resolve_atm_iv
from core.strategy.base import MarketBar

logger = logging.getLogger(__name__)

DEFAULT_RISK_FREE_RATE = 0.07

_FO_ELIGIBLE_TAG = "fo_eligible"
_KITE_FNO_EXCHANGE = "NFO"


def resolve_iv_by_symbol(
    candidate_instruments: list[Instrument],
    price_history: dict[str, list[MarketBar]],
    provider: str,
    groww_credentials: object | None,
    kite_credentials: object | None,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> dict[str, float]:
    """
    Best-effort IV screen: for every ``fo_eligible``-tagged candidate,
    fetch its nearest-expiry option chain from ``provider`` and resolve
    an at-the-money IV.

    A candidate with no ``fo_eligible`` tag, no price history (needed
    for a spot price), or a chain fetch that fails is silently skipped -
    this is a risk-appetite signal, not a required input, so a broker/
    network hiccup here must never block session startup (bucket
    selection falls back to its existing daily-range proxy for any
    symbol missing from the returned dict). ``groww_credentials``/
    ``kite_credentials`` are duck-typed (need only ``.api_key`` plus
    ``.secret``/``.access_token``) so this stays independent of any
    specific app's credential dataclass.
    """
    fo_eligible = [i for i in candidate_instruments if _FO_ELIGIBLE_TAG in i.tags]
    if not fo_eligible:
        return {}

    transport = _build_transport(provider, groww_credentials, kite_credentials)
    if transport is None:
        return {}

    iv_by_symbol: dict[str, float] = {}
    for instrument in fo_eligible:
        bars = price_history.get(instrument.symbol) or []
        if not bars:
            continue
        spot = bars[-1].close
        try:
            iv = _resolve_one(transport, provider, instrument, spot, risk_free_rate)
        except MarketDataError as exc:
            logger.warning("fo_eligible IV screen skipped for %s: %s", instrument.symbol, exc)
            continue
        if iv is not None:
            iv_by_symbol[instrument.symbol] = iv
    return iv_by_symbol


def _build_transport(
    provider: str, groww_credentials: object | None, kite_credentials: object | None
) -> object | None:
    if provider == "groww" and groww_credentials is not None:
        return GrowwMarketDataTransport(
            GrowwMarketDataTransportConfig(
                credentials=GrowwMarketDataCredentials(
                    api_key=groww_credentials.api_key, secret=groww_credentials.secret
                )
            )
        )
    if provider == "kite" and kite_credentials is not None:
        return KiteMarketDataTransport(
            KiteMarketDataTransportConfig(
                credentials=KiteMarketDataCredentials(
                    api_key=kite_credentials.api_key, access_token=kite_credentials.access_token
                )
            )
        )
    return None


def _resolve_one(
    transport: object, provider: str, instrument: Instrument, spot: float, risk_free_rate: float
) -> float | None:
    if provider == "groww":
        expiries = groww_fetch_expiries(transport, instrument.exchange, instrument.symbol)
        if not expiries:
            return None
        snapshot = groww_fetch_option_chain(transport, instrument.exchange, instrument.symbol, expiries[0])
    else:
        expiries = kite_fetch_expiries(transport, _KITE_FNO_EXCHANGE, instrument.symbol)
        if not expiries:
            return None
        snapshot = kite_fetch_option_chain(
            transport, _KITE_FNO_EXCHANGE, instrument.symbol, expiries[0], spot, risk_free_rate
        )
    # Genuine real-time fetch (not a historical bar replay) - date.today()
    # is the semantically correct "as of" here, made explicit at the call
    # site rather than hidden inside resolve_atm_iv.
    return resolve_atm_iv(snapshot, spot, risk_free_rate, as_of=date.today())

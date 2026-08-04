"""
Historical option chain provider, replaying real settled NIFTY/BANKNIFTY
option prices from ``data/processed/nse_index_options_iv_history.csv``
(see ``scripts/fetch_historical_options_nse.py``) instead of a live
broker quote.

The only other ``option_chain_provider`` implementation,
``KiteOptionChainProvider`` (``core/market_data/kite_option_chain.py``),
always fetches "right now" - there is no notion of a replay date in its
``get_chain(underlying_symbol, exchange, expiry_horizon_days, spot)``
contract at all. A multi-day backtest needs to call that same, unchanged
contract once per simulated day and get that day's real chain back -
``set_current_date`` is the caller-driven seam that makes that possible
without touching the shared contract every options strategy already
depends on.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from core.broker.base import OptionType
from core.market_data.option_chain_models import OptionChainContract, OptionChainSnapshot

DEFAULT_CSV_PATH = "data/processed/nse_index_options_iv_history.csv"

_OPTION_TYPE_BY_CSV_CODE = {"PE": OptionType.PUT, "CE": OptionType.CALL}


def _build_tradingsymbol(symbol: str, expiry: str, strike: float, option_type: OptionType) -> str:
    """
    Deterministic, date-independent identity for one contract - the same
    (symbol, expiry, strike, option_type) always produces the same
    string, so a leg opened on one simulated day resolves to the exact
    same tradingsymbol (and therefore the same ``PositionManager`` key)
    when looked up again on a later day.
    """
    return f"{symbol}_{expiry}_{strike:g}_{option_type.value}"


class HistoricalOptionChainProvider:
    """
    Implements the standard ``get_chain`` contract by replaying real
    daily settle prices for whatever date ``set_current_date`` last set.

    Loads the whole CSV once, up front, grouped by
    ``(trade_date, symbol, expiry)`` - real daily-settlement data, not a
    live quote, so ``OptionChainContract.iv`` is populated directly from
    the CSV's own ``implied_vol`` column (no Black-Scholes back-solve
    needed) and ``last_price`` is the settlement price. ``bid``/``ask``
    stay ``None`` (this data source has neither) - every options
    strategy already falls back to ``last_price`` when they're absent
    (see ``OptionChainContract``'s own docstring), so this needs no
    special-casing.

    ``lot_size`` defaults to 1, not a real historical NIFTY/BANKNIFTY lot
    size. ponytail: real lot sizes changed more than once across this
    CSV's 2016-2026 span (see project memory on the Nov 2024/Dec 2025
    hikes) and this dataset carries no lot-size column to derive one
    from; getting that exactly right would need a separate date-keyed
    lot-size table. Doesn't distort profit-factor/win-rate (both legs of
    one trade always share one lot size, whatever it is), only the
    absolute rupee PnL scale - pass an explicit ``lot_size`` if that
    scale needs to be realistic for a specific historical window.
    """

    def __init__(self, csv_path: str | Path = DEFAULT_CSV_PATH, lot_size: int = 1) -> None:
        self._lot_size = lot_size
        self._current_date: date | None = None
        # (trade_date, symbol, expiry) -> contracts observed that day
        self._contracts: dict[tuple[date, str, str], list[OptionChainContract]] = {}
        # (trade_date, symbol) -> sorted expiries observed that day - lets
        # _nearest_expiry_within_horizon do an O(1) lookup + a scan over a
        # handful of expiries instead of an O(len(self._contracts)) filter
        # over every (date, symbol, expiry) key in the whole file on every
        # call (70k+ keys for the full 2016-2026 CSV) - the dominant cost
        # in a multi-day backtest, which calls this once per simulated day.
        self._expiries_by_date_symbol: dict[tuple[date, str], list[str]] = {}
        # (tradingsymbol, trade_date) -> that one contract - the point
        # lookup a multi-day runner needs for an already-open leg, keyed
        # by exactly what PositionManager already tracks it as (its
        # symbol), no re-parsing of the tradingsymbol format needed.
        self._contract_by_tradingsymbol_date: dict[tuple[str, date], OptionChainContract] = {}
        # tradingsymbol -> its last (chronologically latest) known
        # price - see get_price_by_tradingsymbol's docstring for why.
        self._last_price_by_tradingsymbol: dict[str, tuple[date, float]] = {}
        self._load(Path(csv_path))

    def _load(self, path: Path) -> None:
        expiries_seen: dict[tuple[date, str], set[str]] = {}
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            idx = {name: header.index(name) for name in (
                "trade_date", "symbol", "expiry", "strike", "option_type",
                "settle_price", "implied_vol", "open_interest", "volume",
            )}

            for row in reader:
                option_type = _OPTION_TYPE_BY_CSV_CODE.get(row[idx["option_type"]])
                if option_type is None:
                    continue  # not a PE/CE row (defensive - every real row in this file is one)

                trade_date = datetime.strptime(row[idx["trade_date"]], "%Y-%m-%d").date()
                symbol = row[idx["symbol"]]
                expiry = row[idx["expiry"]]
                strike = float(row[idx["strike"]])
                implied_vol = row[idx["implied_vol"]]

                contract = OptionChainContract(
                    underlying_symbol=symbol,
                    strike=strike,
                    expiry=expiry,
                    option_type=option_type,
                    tradingsymbol=_build_tradingsymbol(symbol, expiry, strike, option_type),
                    last_price=float(row[idx["settle_price"]]),
                    iv=float(implied_vol) if implied_vol else None,
                    lot_size=self._lot_size,
                    open_interest=float(row[idx["open_interest"]]) if row[idx["open_interest"]] else None,
                    volume=float(row[idx["volume"]]) if row[idx["volume"]] else None,
                )
                self._contracts.setdefault((trade_date, symbol, expiry), []).append(contract)
                self._contract_by_tradingsymbol_date[(contract.tradingsymbol, trade_date)] = contract
                expiries_seen.setdefault((trade_date, symbol), set()).add(expiry)

                latest = self._last_price_by_tradingsymbol.get(contract.tradingsymbol)
                if latest is None or trade_date > latest[0]:
                    self._last_price_by_tradingsymbol[contract.tradingsymbol] = (trade_date, contract.last_price)

        self._expiries_by_date_symbol = {key: sorted(expiries) for key, expiries in expiries_seen.items()}

    def set_current_date(self, as_of: date) -> None:
        """Set which trade_date subsequent get_chain()/get_contract_price() calls replay."""
        self._current_date = as_of

    def get_chain(
        self, underlying_symbol: str, exchange: str, expiry_horizon_days: int, spot: float
    ) -> OptionChainSnapshot | None:
        """Same contract as KiteOptionChainProvider.get_chain - see module docstring."""
        if self._current_date is None:
            raise RuntimeError(
                "HistoricalOptionChainProvider.get_chain called before set_current_date() - "
                "a caller replaying historical bars must say which date it's replaying."
            )

        expiry = self._nearest_expiry_within_horizon(underlying_symbol, expiry_horizon_days)
        if expiry is None:
            return None

        contracts = self._contracts[(self._current_date, underlying_symbol, expiry)]
        return OptionChainSnapshot(
            underlying_symbol=underlying_symbol,
            underlying_last_price=spot,
            expiry=expiry,
            contracts=list(contracts),
        )

    def get_contract_price(
        self, underlying_symbol: str, expiry: str, strike: float, option_type: OptionType, as_of: date
    ) -> float | None:
        """
        Real settle price for one specific, already-open contract on
        ``as_of`` - what a multi-day runner uses to build a synthetic
        exit-evaluation bar for a leg carried over from an earlier
        simulated day. Delegates to ``get_price_by_tradingsymbol`` (see
        its docstring for what happens once ``as_of`` is past this
        contract's own last row) - same lookup, just addressed by its
        component fields instead of an already-known tradingsymbol.
        """
        tradingsymbol = _build_tradingsymbol(underlying_symbol, expiry, strike, option_type)
        return self.get_price_by_tradingsymbol(tradingsymbol, as_of)

    def get_price_by_tradingsymbol(self, tradingsymbol: str, as_of: date) -> float | None:
        """
        Real settle price on ``as_of`` for a contract already identified
        by its ``tradingsymbol`` (e.g. a leg ``PositionManager`` is
        already tracking from an earlier simulated day) - what
        ``core.backtest.multi_day_position_runner`` uses to build that
        day's synthetic exit-evaluation bar for a carried leg, without
        needing to remember or re-derive its strike/expiry/option_type
        separately.

        Falls back to the contract's own last (chronologically latest)
        known price once ``as_of`` is past its last real row - a real
        option stops appearing in this source data once it expires;
        without this fallback, a still-open leg held past its own
        contract's last trade date would get ``None`` forever and never
        be exit-evaluated again, silently stuck open for the rest of a
        multi-day backtest. Deliberately a simplification, not real
        settlement math (a cash-settled index option's true value at/
        after expiry is intrinsic value against final spot, not a frozen
        premium) - documented here rather than modeled, since getting it
        exactly right needs the settlement-price convention verified
        against real NSE circulars, not assumed.

        ``None`` only for a tradingsymbol this provider has never seen
        at all (e.g. a typo, or a contract that was never in the source
        data to begin with).
        """
        contract = self._contract_by_tradingsymbol_date.get((tradingsymbol, as_of))
        if contract is not None:
            return contract.last_price
        latest = self._last_price_by_tradingsymbol.get(tradingsymbol)
        return latest[1] if latest is not None else None

    def _nearest_expiry_within_horizon(self, underlying_symbol: str, expiry_horizon_days: int) -> str | None:
        candidates = self._expiries_by_date_symbol.get((self._current_date, underlying_symbol), [])
        for expiry in candidates:
            days_to_expiry = (date.fromisoformat(expiry) - self._current_date).days
            if 0 <= days_to_expiry <= expiry_horizon_days:
                return expiry
        return None

"""
The expensive part of deciding what to trade today: Angel One candidate
prescreen (core/market_data/candidate_prescreen.py) + historical fetch +
capital/risk-appetite bucket selection (core/market_data/bucket_selector.py).

Extracted out of apps/live_trader/main.py's build_live_session so it has
exactly one implementation shared by two callers:

- build_live_session itself, when no overnight-precomputed selection
  exists for today (the live fallback path - selection happens right now,
  same as before this module existed).
- apps/live_trader/select_candidates_main.py, the overnight entry point
  that runs this once and persists the result (see
  core/market_data/candidate_selection_store.py) so the market-open
  trading session can skip straight to it instead of repeating this
  starting at 9 AM.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from apps.live_trader.config import AngelOneEnvCredentials, GrowwEnvCredentials, KiteEnvCredentials, LiveTraderConfig
from core.allocations.daily_mandate import DailyMandate
from core.market_data.angelone_transport import (
    AngelOneMarketDataCredentials,
    AngelOneMarketDataTransport,
    AngelOneMarketDataTransportConfig,
)
from core.market_data.bucket_selector import BucketSelectionCriteria, BucketSelectionResult, CapitalRiskBucketSelector
from core.market_data.candidate_prescreen import PrescreenCriteria, PrescreenResult, prescreen_candidates
from core.market_data.candidate_profile import CandidateProfile
from core.market_data.factory import build_market_data_feed
from core.market_data.factory_models import MarketDataConfig
from core.market_data.fo_iv_screen import resolve_iv_by_symbol
from core.market_data.universe_models import Instrument
from core.market_data.universe_screener import LiquidityTrendScreeningCriteria, screen_by_liquidity_and_trend
from core.portfolio.models import PortfolioAllocationPlan
from core.strategy.base import MarketBar

# (open_delta, high_delta, low_delta, close_delta, volume) per bar,
# relative to each instrument's base_price: 5 quiet consolidation bars
# (stable, highs capped around +5) establishing a real reference high,
# then a breakout bar (closes above that reference high, volume spike),
# then a bar that rallies well past the exit engine's profit target.
# 5 quiet bars matches simple_breakout_momentum's default
# breakout_lookback_bars=5 (see that module's docstring) - fewer bars of
# history than the lookback needs and the strategy never fires at all.
_DEMO_BAR_SHAPE: list[tuple[float, float, float, float, float]] = [
    (-2.0, 3.0, -8.0, -1.0, 90000),
    (-1.0, 4.0, -7.0, 0.0, 92000),
    (-2.0, 5.0, -8.0, 1.0, 91000),
    (-1.0, 3.0, -6.0, -1.0, 93000),
    (0.0, 4.0, -7.0, 0.0, 90000),
    (0.0, 25.0, -3.0, 20.0, 160000),  # breakout: closes above the ~+5 reference high
    (22.0, 225.0, 15.0, 220.0, 120000),  # rallies well past the profit target
]


def build_session_bars(instruments: list[Instrument]) -> list[MarketBar]:
    """
    Build a small deterministic, interleaved multi-symbol intraday bar
    stream, standing in for a live feed.

    This is the same demo shape used by the paper trader (see
    ``_DEMO_BAR_SHAPE``: 5 quiet consolidation bars, a breakout bar, then
    a bar that rallies past the exit engine's profit target, per
    instrument at a distinct price level). Used only for the safe
    local/dev market-data path (``LIVE_MARKET_DATA_PROVIDER=memory``, the
    default) - see ``apps/live_trader/main.py``'s ``_build_market_data_config``.
    The real live paths never touch these bars.
    """
    base_time = datetime.now().replace(second=0, microsecond=0)
    bars: list[MarketBar] = []

    for offset, (open_delta, high_delta, low_delta, close_delta, volume) in enumerate(_DEMO_BAR_SHAPE):
        timestamp = base_time + timedelta(minutes=offset)
        for index, instrument in enumerate(instruments):
            base_price = 500.0 + index * 300.0

            bars.append(
                MarketBar(
                    symbol=instrument.symbol,
                    exchange=instrument.exchange,
                    timestamp=timestamp,
                    open=base_price + open_delta,
                    high=base_price + high_delta,
                    low=base_price + low_delta,
                    close=base_price + close_delta,
                    volume=volume,
                )
            )

    return bars


def select_session_instruments(
    live_config: LiveTraderConfig,
    candidate_instruments: list[Instrument],
    mandate: DailyMandate,
    allocation_plan: PortfolioAllocationPlan,
    groww_credentials: GrowwEnvCredentials | None,
    kite_credentials: KiteEnvCredentials | None,
    angelone_credentials: AngelOneEnvCredentials | None,
    candidate_profile: CandidateProfile | None = None,
) -> tuple[list[Instrument], PrescreenResult | None, BucketSelectionResult]:
    """
    Narrow ``candidate_instruments`` (every enabled instrument on the
    configured exchange) down to today's tradable bucket.

    ``candidate_profile`` is the configured strategy's declared shape of
    candidate it wants (see ``core.market_data.candidate_profile.
    CandidateProfile``) - ``None`` (default) reproduces the exact
    pre-existing behavior (affordability + volatility ceiling only,
    source-order truncation). This is what makes selection
    strategy-aware instead of one generic screen every strategy shares.

    Returns ``(candidates_after_prescreen, prescreen_result,
    bucket_result)`` - ``prescreen_result`` is ``None`` for every
    provider except Angel One (see below).

    Angel One only: pre-screens the (potentially huge - the full NSE cash
    universe is ~2400 symbols) candidate list down to a small batch
    BEFORE paying for a full historical fetch per candidate - see
    core/market_data/candidate_prescreen.py's module docstring for why
    this is a real strategy-shaping decision, not just a speed
    optimization (only symbols with real news coverage today survive).
    Skipped entirely for every other provider - their candidate lists are
    small enough that the historical fetch below is already fine.
    """
    prescreen_result: PrescreenResult | None = None
    if live_config.market_data_provider == "angelone":
        prescreen_transport = AngelOneMarketDataTransport(
            AngelOneMarketDataTransportConfig(
                credentials=AngelOneMarketDataCredentials(
                    api_key=angelone_credentials.api_key,
                    client_code=angelone_credentials.client_code,
                    password=angelone_credentials.password,
                    totp_secret=angelone_credentials.totp_secret,
                )
            )
        )
        prescreen_result = prescreen_candidates(
            candidate_instruments,
            prescreen_transport,
            PrescreenCriteria(max_capital_per_trade=allocation_plan.max_capital_per_trade),
        )
        candidate_instruments = prescreen_result.survivors

    # Capital-affordability + risk-appetite volatility screen (see
    # core/market_data/bucket_selector.py). Skipped entirely when
    # prescreen left zero candidates (a real, non-error outcome - a quiet
    # news day can legitimately clear the whole affordable set) rather
    # than calling the historical fetch with an empty instrument list,
    # which the factory correctly refuses (a historical request for zero
    # symbols isn't meaningful) but would surface as a confusing crash
    # here, not a quiet no-op.
    if not candidate_instruments:
        return candidate_instruments, prescreen_result, BucketSelectionResult()

    candidate_market_data_config = _build_candidate_market_data_config(
        live_config, candidate_instruments, groww_credentials, kite_credentials, angelone_credentials
    )
    candidate_feed = build_market_data_feed(candidate_market_data_config)
    price_history = {
        instrument.symbol: candidate_feed.get_history(instrument.symbol)
        for instrument in candidate_instruments
    }

    profile = candidate_profile or CandidateProfile()
    if profile.require_liquidity_and_uptrend:
        # Extra strategy-declared pass ahead of the generic bucket
        # selector: liquidity floor + "already in an uptrend" (see
        # core/market_data/universe_screener.py) - previously built,
        # tested, and never wired into the daily selection pipeline at
        # all (only scripts/refresh_universe.py could have used it,
        # offline). This is what makes a momentum/breakout/trend
        # strategy's daily bucket actually contain trending, liquid
        # names instead of whatever cleared affordability + news first.
        trend_screening = screen_by_liquidity_and_trend(
            candidate_instruments,
            price_history,
            LiquidityTrendScreeningCriteria(
                min_avg_daily_volume=profile.min_avg_volume,
                trend_lookback_bars=profile.trend_lookback_bars,
            ),
        )
        candidate_instruments = trend_screening.selected
        if not candidate_instruments:
            return candidate_instruments, prescreen_result, BucketSelectionResult()

    bucket_criteria = BucketSelectionCriteria(
        risk_appetite=mandate.risk_appetite,
        max_capital_per_trade=allocation_plan.max_capital_per_trade,
        max_bucket_size=allocation_plan.max_open_positions,
        rank_by_volatility=profile.rank_by_volatility,
    )
    # Best-effort options-implied-volatility screen for fo_eligible
    # candidates (see core/market_data/fo_iv_screen.py) - a broker/
    # network hiccup here degrades to the existing daily-range proxy,
    # never blocks selection.
    iv_by_symbol = resolve_iv_by_symbol(
        candidate_instruments,
        price_history,
        live_config.market_data_provider,
        groww_credentials,
        kite_credentials,
    )
    bucket_result = CapitalRiskBucketSelector().select(
        candidate_instruments, price_history, bucket_criteria, iv_by_symbol
    )
    return candidate_instruments, prescreen_result, bucket_result


def _build_candidate_market_data_config(
    live_config: LiveTraderConfig,
    candidate_instruments: list[Instrument],
    groww_credentials: GrowwEnvCredentials | None,
    kite_credentials: KiteEnvCredentials | None,
    angelone_credentials: AngelOneEnvCredentials | None,
) -> MarketDataConfig:
    """
    Build the ``MarketDataConfig`` covering every *candidate* instrument,
    used only to fetch the price history bucket selection needs - mirrors
    ``apps/paper_trader/main.py``'s identical helper.

    ``"groww"``/``"kite"``/``"angelone"`` fetch a short window of real
    historical candles (``live_config.historical_from_date``/``to_date``/
    ``interval``) purely for this affordability/volatility read.
    ``"memory"`` reuses synthetic demo bars so bucket selection runs
    identically (just against synthetic data) with no network access at
    all.
    """
    if live_config.market_data_provider == "angelone":
        return MarketDataConfig(
            provider="angelone",
            mode="historical",
            metadata={
                "api_key": angelone_credentials.api_key,
                "client_code": angelone_credentials.client_code,
                "password": angelone_credentials.password,
                "totp_secret": angelone_credentials.totp_secret,
                "instruments": [
                    {"symbol": instrument.symbol, "exchange": instrument.exchange}
                    for instrument in candidate_instruments
                ],
                "from_date": live_config.historical_from_date,
                "to_date": live_config.historical_to_date,
                "interval": live_config.historical_interval,
            },
        )

    if live_config.market_data_provider == "groww":
        return MarketDataConfig(
            provider="groww",
            mode="historical",
            metadata={
                "api_key": groww_credentials.api_key,
                "secret": groww_credentials.secret,
                "instruments": [
                    {"symbol": instrument.symbol, "exchange": instrument.exchange}
                    for instrument in candidate_instruments
                ],
                "from_date": live_config.historical_from_date,
                "to_date": live_config.historical_to_date,
                "interval": live_config.historical_interval,
            },
        )

    if live_config.market_data_provider == "kite":
        return MarketDataConfig(
            provider="kite",
            mode="historical",
            metadata={
                "api_key": kite_credentials.api_key,
                "access_token": kite_credentials.access_token,
                "instruments": [
                    {"symbol": instrument.symbol, "exchange": instrument.exchange}
                    for instrument in candidate_instruments
                ],
                "from_date": live_config.historical_from_date,
                "to_date": live_config.historical_to_date,
                "interval": live_config.historical_interval,
            },
        )

    return MarketDataConfig(
        provider="memory",
        mode="paper",
        metadata={"bars": build_session_bars(candidate_instruments)},
    )

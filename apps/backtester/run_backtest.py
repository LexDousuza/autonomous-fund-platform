"""
Backtester V1: runs a strategy through the SAME ``ExecutionOrchestrator``
pipeline paper/live trading use (position sizing, exit rules, session
risk guardrails, portfolio exposure limits), one trading day at a time,
over historical bars - see ``core/backtest/day_runner.py``. This
replaces the earlier ``SimpleBacktestEngine``, which had its own
separate, much simpler rules (fixed quantity, no exit engine, no risk
guardrails, one continuous run instead of daily resets) that didn't
predict how a strategy would actually behave in paper/live trading.

Data source (``BACKTEST_DATA_SOURCE``, default ``"synthetic"``):

- ``"synthetic"``: deterministic seeded random-walk bars for the full
  configured NSE universe (see ``core/backtest/synthetic_data.py``) -
  this is NOT real historical price data, and a report built from it is
  a test of system/strategy *mechanics*, not a profitability prediction.
  Configurable via ``BACKTEST_START_DATE``/``BACKTEST_NUM_DAYS``/
  ``BACKTEST_INTERVAL_MINUTES``/``BACKTEST_SEED``.
- ``"csv"``: bars loaded from ``BACKTEST_CSV_PATH`` (default: the small
  bundled sample file) - the traded universe is whatever symbols the
  file actually contains, not the full declared universe.
- ``"groww"``: real historical candles fetched from Groww for the full
  configured NSE universe (see ``core/market_data/groww_historical_feed.py``)
  - this IS real historical price data, and a report built from it is a
  genuine (if still simplified - see below) backtest, not just a
  mechanics check. Requires ``GROWW_API_KEY``/``GROWW_API_SECRET`` in
  the environment plus ``BACKTEST_HISTORICAL_FROM_DATE``/
  ``BACKTEST_HISTORICAL_TO_DATE`` (``"YYYY-MM-DD"``); optionally
  ``BACKTEST_HISTORICAL_INTERVAL`` (default ``"1day"``). CAVEAT: the
  traded universe still comes from ``UNIVERSE_PATH`` - today's
  instrument list, not a point-in-time constituent snapshot for the
  requested date range. No point-in-time constituent history is wired
  into this codebase yet, so a backtest over 2022 still uses 2026's
  list; delisted/removed names are invisibly excluded
  (survivorship bias). ``print_report`` surfaces this caveat whenever
  ``data_source="groww"`` rather than staying silent about it.

Fill realism: every data source runs through ``PaperBroker`` via
``run_multi_day_backtest``, which now applies ``BACKTEST_SLIPPAGE_BPS``
(default ``5.0``, i.e. 0.05%) against the trader on every fill - real
trades don't fill at the exact requested price with zero cost. Set to
``0`` to reproduce the old (unrealistic) exact-fill behavior. Every
closed trade's ``realized_pnl`` is also net of brokerage/STT/exchange
charges now (see ``core/backtest/costs.py``) - previously gross, as if
every fill were free beyond just the requested price.

Bucket selection (synthetic/groww only - ``"csv"`` skips it, see above):
the earliest ``BACKTEST_SELECTION_LOOKBACK_DAYS`` calendar days (default
10) of fetched bars are used ONLY to pick the tradable bucket
(``_select_bucket``), and are excluded from the actual backtest - the
remaining days are what gets traded and reported on. Previously the
bucket was chosen from the *entire* fetched window, including days after
the ones being decided for - a look-ahead bug (a bucket partly chosen
from future volatility isn't a decision the strategy could actually have
made on day 1).
"""

from __future__ import annotations

import os
from datetime import date

from apps.live_trader.config import load_groww_credentials_from_env
from apps.live_trader.main import build_exit_policy_for_strategy, check_holding_period_is_supported
from core.allocations.daily_mandate import DailyMandate, load_daily_mandate
from core.backtest.day_runner import run_multi_day_backtest
from core.backtest.engine import load_bars_from_csv
from core.backtest.report import BacktestPerformanceReport, build_performance_report
from core.backtest.synthetic_data import SyntheticSymbolConfig, generate_synthetic_bars
from core.market_data.candidate_profile import CandidateProfile
from core.market_data.bucket_selector import (
    BucketSelectionCriteria,
    BucketSelectionResult,
    CapitalRiskBucketSelector,
)
from core.market_data.factory import build_market_data_feed
from core.market_data.factory_models import MarketDataConfig
from core.market_data.universe_loader import load_instrument_universe
from core.market_data.universe_models import Instrument
from core.market_data.universe_selector import TradingUniverseSelector, UniverseSelectionCriteria
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.portfolio.models import PortfolioAllocationPlan
from core.pricing.market_proxy import EqualWeightedMarketProxy
from core.portfolio.exit_models import ExitPolicyConfig, parse_time_of_day
from core.risk.simple_intraday import (
    DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES,
    IntradayRiskSettings,
    SimpleIntradayRiskManager,
)
from core.strategy.base import MarketBar
import core.strategy.gbm_momentum  # noqa: F401  (registers the strategy)
import core.strategy.ma_crossover  # noqa: F401  (registers the strategy)
import core.strategy.momentum  # noqa: F401  (registers the strategy)
import core.strategy.ornstein_uhlenbeck  # noqa: F401  (registers the strategy)
import core.strategy.ornstein_uhlenbeck_residual  # noqa: F401  (registers the strategy)
import core.strategy.pullback  # noqa: F401  (registers the strategy)
import core.strategy.relative_volume_pullback  # noqa: F401  (registers the strategy)
import core.strategy.simple_breakout_momentum  # noqa: F401  (registers the strategy)
import core.strategy.vwap_reversion  # noqa: F401  (registers the strategy)

_STRATEGIES_NEEDING_MARKET_INDEX_FEED = {"ornstein_uhlenbeck_residual"}
from core.utils.config_loader import load_yaml_file

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
UNIVERSE_PATH = "config/instruments/india_cash_universe.yaml"
STRATEGY_CONFIG_PATH = os.environ.get(
    "BACKTEST_STRATEGY_CONFIG_PATH", "config/strategies/simple_breakout_momentum.yaml"
)
DEFAULT_CSV_PATH = "data/backtests/sample_reliance_1m.csv"
_SUPPORTED_DATA_SOURCES = {"synthetic", "csv", "groww"}

# Rough, illustrative starting prices for the synthetic generator - not
# real quotes, just plausible order-of-magnitude levels per symbol so
# different large-caps don't all start identically.
_SYNTHETIC_STARTING_PRICES: dict[str, float] = {
    "RELIANCE": 2500.0,
    "TCS": 3800.0,
    "INFY": 1500.0,
    "HDFCBANK": 1650.0,
    "ICICIBANK": 1100.0,
    "SBIN": 800.0,
    "ITC": 450.0,
    "LT": 3600.0,
}


def _load_synthetic_bars(instruments: list[Instrument]) -> list[MarketBar]:
    start_date = date.fromisoformat(os.environ.get("BACKTEST_START_DATE", "2026-01-05"))
    num_days = int(os.environ.get("BACKTEST_NUM_DAYS", "30"))
    interval_minutes = int(os.environ.get("BACKTEST_INTERVAL_MINUTES", "5"))
    seed = int(os.environ.get("BACKTEST_SEED", "42"))
    # Default 0.0 = pure random walk (no presumed exploitable structure).
    # See SyntheticSymbolConfig's docstring before changing this.
    momentum_strength = float(os.environ.get("BACKTEST_MOMENTUM_STRENGTH", "0.0"))

    symbol_configs = [
        SyntheticSymbolConfig(
            symbol=instrument.symbol,
            exchange=instrument.exchange,
            starting_price=_SYNTHETIC_STARTING_PRICES.get(instrument.symbol, 1000.0),
            momentum_strength=momentum_strength,
        )
        for instrument in instruments
    ]
    return generate_synthetic_bars(symbol_configs, start_date, num_days, interval_minutes, seed)


def _load_groww_historical_bars(instruments: list[Instrument]) -> list[MarketBar]:
    """
    Fetch real historical candles for ``instruments`` from Groww, via the
    same ``build_market_data_feed`` factory ``apps/paper_trader/main.py``
    already uses for its ``"groww"`` provider - see
    ``core/market_data/groww_historical_feed.py``.
    """
    credentials = load_groww_credentials_from_env()
    from_date = os.environ.get("BACKTEST_HISTORICAL_FROM_DATE", "").strip()
    to_date = os.environ.get("BACKTEST_HISTORICAL_TO_DATE", "").strip()
    if not from_date or not to_date:
        raise ValueError(
            "BACKTEST_DATA_SOURCE=groww requires BACKTEST_HISTORICAL_FROM_DATE "
            "and BACKTEST_HISTORICAL_TO_DATE ('YYYY-MM-DD')."
        )
    interval = os.environ.get("BACKTEST_HISTORICAL_INTERVAL", "1day")

    market_data_config = MarketDataConfig(
        provider="groww",
        mode="historical",
        metadata={
            "api_key": credentials.api_key,
            "secret": credentials.secret,
            "instruments": [
                {"symbol": instrument.symbol, "exchange": instrument.exchange}
                for instrument in instruments
            ],
            "from_date": from_date,
            "to_date": to_date,
            "interval": interval,
        },
    )
    feed = build_market_data_feed(market_data_config)
    return sorted(
        (bar for instrument in instruments for bar in feed.get_history(instrument.symbol)),
        key=lambda bar: (bar.timestamp, bar.symbol),
    )


def _select_bucket(
    candidate_instruments: list[Instrument],
    bars: list[MarketBar],
    mandate: DailyMandate,
    allocation_plan: PortfolioAllocationPlan,
    candidate_profile: CandidateProfile,
) -> BucketSelectionResult:
    """
    Apply the same capital-affordability + risk-appetite volatility screen
    apps/paper_trader/main.py and apps/live_trader/main.py already run
    (see core/market_data/bucket_selector.py) - previously skipped here
    entirely, meaning a backtest could report performance for a symbol
    neither paper nor live trading would ever have actually traded that
    day.

    ``bars`` covers the full fetched window, but only its EARLIEST
    ``BACKTEST_SELECTION_LOOKBACK_DAYS`` calendar days (default 10,
    matching apps/live_trader's own bucket-selection lookback) feed this
    volatility read - the remaining days are what actually gets traded
    (see main()'s split of candidate_bars into a selection prefix and a
    trading suffix). This used to see the *whole* backtest window,
    including days after the ones it was deciding for - a real look-ahead
    bug, not a "measures mechanics, not profitability" simplification:
    a bucket chosen partly from future volatility isn't a decision the
    strategy could actually have made on day 1.

    ``candidate_profile.rank_by_volatility`` is threaded into
    ``BucketSelectionCriteria`` here so a capped bucket keeps the
    highest-volatility survivors, matching apps/paper_trader/main.py's
    ``_select_strategy_bucket`` - previously dropped on the floor, so the
    flag had zero effect in backtests regardless of what a strategy's
    YAML declared.
    """
    price_history: dict[str, list[MarketBar]] = {}
    for bar in bars:
        price_history.setdefault(bar.symbol, []).append(bar)

    bucket_criteria = BucketSelectionCriteria(
        risk_appetite=mandate.risk_appetite,
        max_capital_per_trade=allocation_plan.max_capital_per_trade,
        max_bucket_size=allocation_plan.max_open_positions,
        rank_by_volatility=candidate_profile.rank_by_volatility,
    )
    return CapitalRiskBucketSelector().select(candidate_instruments, price_history, bucket_criteria)


def print_report(
    report: BacktestPerformanceReport,
    strategy_name: str,
    strategy_params: dict,
    data_source: str,
) -> None:
    print("=== BACKTEST RESULT ===")
    print(f"Strategy: {strategy_name}")
    print(f"Params: {strategy_params}")
    print(f"Data source: {data_source}")
    if data_source == "synthetic":
        print(
            "NOTE: synthetic data is a seeded random walk, NOT real "
            "historical prices - this measures strategy/system mechanics, "
            "not real-world profitability."
        )
    if data_source == "groww":
        print(
            "NOTE: real historical prices, but the traded universe is "
            f"TODAY'S instrument list ({UNIVERSE_PATH}), not a "
            "point-in-time constituent snapshot - no point-in-time "
            "constituent history is wired into this codebase yet, so "
            "delisted/removed names for this date range are invisibly "
            "excluded (survivorship bias)."
        )
    print()

    print(f"Trading days: {report.trading_days}")
    print(f"Total closed trades: {report.total_trades}")
    print(f"Winning trades: {report.winning_trades}")
    print(f"Losing trades: {report.losing_trades}")
    print(f"Breakeven trades: {report.breakeven_trades}")
    print(f"Win rate: {report.win_rate:.1%}")
    print(f"Total realized PnL (net of costs): {report.total_realized_pnl:.2f}")
    print(f"Total costs (brokerage+STT+exchange charges, excl. slippage): {report.total_costs:.2f}")
    print(f"Average PnL per trade: {report.average_pnl_per_trade:.2f}")
    print(f"Average win: {report.average_win:.2f}")
    print(f"Average loss: {report.average_loss:.2f}")
    print(f"Profit factor: {report.profit_factor if report.profit_factor is not None else 'n/a (no losses)'}")
    print(f"Max drawdown: {report.max_drawdown:.2f}")
    print(f"Duplicate orders blocked: {report.duplicate_orders_blocked}")
    print(f"Exit reasons: {report.exit_reason_counts}")
    print()

    if report.closed_trades:
        print("=== TRADE LOG ===")
        for i, trade in enumerate(report.closed_trades, start=1):
            print(
                f"{i}. {trade.trading_date} {trade.exchange}:{trade.symbol} | "
                f"entry={trade.entry_price} exit={trade.exit_price} | "
                f"qty={trade.quantity} | reason={trade.exit_reason} | "
                f"pnl={trade.realized_pnl:.2f}"
            )
    else:
        print("No trades generated")


def main() -> None:
    data_source = os.environ.get("BACKTEST_DATA_SOURCE", "synthetic").strip().lower()
    if data_source not in _SUPPORTED_DATA_SOURCES:
        raise ValueError(
            f"Unsupported BACKTEST_DATA_SOURCE '{data_source}'. "
            f"Supported: {sorted(_SUPPORTED_DATA_SOURCES)}"
        )

    mandate = load_daily_mandate(MANDATE_PATH)
    if "BACKTEST_INTRADAY_CAPITAL" in os.environ:
        # sample_mandate.yaml's Rs 10,000 is a paper-trading demo figure,
        # not sized for realistic cost economics: at the resulting
        # max_capital_per_trade (~Rs 2,500), Groww's Rs 5 per-leg brokerage
        # floor (core/broker/groww_fees.py) alone is ~0.4-0.5% of notional
        # round-trip - larger than the entry deviations a significance-
        # gated mean-reversion strategy typically finds (see
        # docs/strategy-specs/ornstein_uhlenbeck.md), making costs
        # structurally dominate regardless of exit-band tuning. Research-
        # only override, same in-memory-only convention as
        # apps/paper_trader/main.py's _apply_config_overrides - the
        # mandate file itself is never touched.
        mandate.buckets.intraday_bucket = float(os.environ["BACKTEST_INTRADAY_CAPITAL"])
        mandate.validate()
    allocation_plan = build_portfolio_allocation_plan(mandate)

    strategy_cfg = load_yaml_file(STRATEGY_CONFIG_PATH)
    strategy_name = strategy_cfg["strategy"]["name"]
    check_holding_period_is_supported(strategy_name)
    strategy_params = strategy_cfg["params"]
    # Same candidate_profile: block apps/live_trader/main.py's
    # load_strategy_config reads - previously never exercised in a
    # backtest at all (see core/backtest/day_runner.py's docstring).
    candidate_profile = CandidateProfile(**strategy_cfg.get("candidate_profile", {}))

    if data_source == "csv":
        # Deliberately skips bucket selection: the traded universe here is
        # already constrained to whatever symbols the file contains, not
        # the full declared universe - see this module's docstring.
        csv_path = os.environ.get("BACKTEST_CSV_PATH", DEFAULT_CSV_PATH)
        bars = load_bars_from_csv(csv_path)
        symbols = sorted({bar.symbol for bar in bars})
    else:
        universe = load_instrument_universe(UNIVERSE_PATH)
        candidate_instruments = TradingUniverseSelector().select(
            universe, UniverseSelectionCriteria(exchange="NSE")
        )
        candidate_bars = (
            _load_groww_historical_bars(candidate_instruments)
            if data_source == "groww"
            else _load_synthetic_bars(candidate_instruments)
        )

        # Split into a selection-only prefix (never traded, never reported
        # on) and the actual trading window - see this module's docstring
        # on why the bucket must not be chosen using days it also gets
        # evaluated against.
        selection_lookback_days = int(os.environ.get("BACKTEST_SELECTION_LOOKBACK_DAYS", "10"))
        distinct_dates = sorted({bar.timestamp.date() for bar in candidate_bars})
        if len(distinct_dates) <= selection_lookback_days:
            raise ValueError(
                f"Only {len(distinct_dates)} distinct day(s) of data available, but "
                f"BACKTEST_SELECTION_LOOKBACK_DAYS={selection_lookback_days} needs at "
                "least one trading day left over after the selection window."
            )
        selection_cutoff_date = distinct_dates[selection_lookback_days - 1]
        selection_bars = [bar for bar in candidate_bars if bar.timestamp.date() <= selection_cutoff_date]
        trading_bars = [bar for bar in candidate_bars if bar.timestamp.date() > selection_cutoff_date]

        bucket_result = _select_bucket(
            candidate_instruments, selection_bars, mandate, allocation_plan, candidate_profile
        )
        bucket_symbols = {instrument.symbol for instrument in bucket_result.selected}

        print("=== BUCKET SELECTION ===")
        print(f"Selection window: {distinct_dates[0]} to {selection_cutoff_date} "
              f"({selection_lookback_days} days, excluded from trading below)")
        print(f"Candidates considered ({len(candidate_instruments)}): "
              f"{', '.join(i.symbol for i in candidate_instruments)}")
        print(f"Bucket selected ({len(bucket_result.selected)}): "
              f"{', '.join(sorted(bucket_symbols)) or 'none'}")
        if bucket_result.excluded:
            print(f"Excluded ({len(bucket_result.excluded)}):")
            for exclusion in bucket_result.excluded:
                print(f"  -> {exclusion.exchange}:{exclusion.symbol} reason={exclusion.reason}")
        print()

        bars = [bar for bar in trading_bars if bar.symbol in bucket_symbols]
        symbols = [instrument.symbol for instrument in bucket_result.selected]

    if strategy_name in _STRATEGIES_NEEDING_MARKET_INDEX_FEED:
        # No real intraday index feed is wired into backtesting (see
        # core/pricing/market_proxy.py) - build the proxy from every
        # symbol this backtest already loaded, same as
        # options_mispricing/credit_spread inject option_chain_provider:
        # a live object via strategy_params, not a YAML-serializable value.
        bars_by_symbol: dict[str, list[MarketBar]] = {}
        for bar in bars:
            bars_by_symbol.setdefault(bar.symbol, []).append(bar)
        # An optional params["pair_symbol"] turns this from "residual
        # against a broad basket" into "residual against one specific
        # other instrument" (a pairs trade) - same strategy class and
        # injection mechanism either way; only which bars build the proxy
        # differs. See config/strategies/ornstein_uhlenbeck_pair.yaml.
        pair_symbol = strategy_params.get("pair_symbol")
        if pair_symbol:
            bars_by_symbol = {pair_symbol: bars_by_symbol[pair_symbol]} if pair_symbol in bars_by_symbol else {}
        strategy_params = {**strategy_params, "market_index_feed": EqualWeightedMarketProxy(bars_by_symbol)}

    square_off_time = (
        parse_time_of_day(mandate.risk_limits.force_square_off_at)
        if mandate.risk_limits.force_square_off_at
        else None
    )
    if "BACKTEST_BREAKOUT_LOOKBACK_BARS" in os.environ:
        strategy_params = {
            **strategy_params,
            "breakout_lookback_bars": int(os.environ["BACKTEST_BREAKOUT_LOOKBACK_BARS"]),
        }
    # See apps/live_trader/main.py's build_exit_policy_for_strategy: most
    # strategies get a flat stop-loss/target; a strategy whose own design
    # requires square_off_time alone gets exactly that instead.
    # BACKTEST_STOP_LOSS_PCT / BACKTEST_TARGET_PCT then override the flat
    # default for quick A/B comparisons during strategy research - unset,
    # they reproduce apps/paper_trader's and apps/live_trader's actual
    # hardcoded exit levels exactly. Never applied to a square-off-only
    # strategy, regardless of these env vars - that policy is a documented
    # safety requirement, not a tunable default.
    exit_policy = build_exit_policy_for_strategy(strategy_name, square_off_time)
    if exit_policy.stop_loss_pct is not None or exit_policy.target_pct is not None:
        exit_policy = ExitPolicyConfig(
            stop_loss_pct=float(os.environ.get("BACKTEST_STOP_LOSS_PCT", str(exit_policy.stop_loss_pct))),
            target_pct=float(os.environ.get("BACKTEST_TARGET_PCT", str(exit_policy.target_pct))),
            square_off_time=square_off_time,
        )

    slippage_bps = float(os.environ.get("BACKTEST_SLIPPAGE_BPS", "5.0"))

    day_results = run_multi_day_backtest(
        bars=bars,
        symbols=symbols,
        allocation_plan=allocation_plan,
        strategy_name=strategy_name,
        strategy_params=strategy_params,
        exit_policy=exit_policy,
        risk_manager=SimpleIntradayRiskManager(
            IntradayRiskSettings(
                min_profit_to_brokerage_ratio_by_strategy=DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES
            )
        ),
        slippage_bps=slippage_bps,
        candidate_profile=candidate_profile,
    )

    report = build_performance_report(day_results)
    print_report(report, strategy_name, strategy_params, data_source)


if __name__ == "__main__":
    main()

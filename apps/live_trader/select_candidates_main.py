"""
Overnight candidate-selection entry point: runs the expensive prescreen +
historical fetch + bucket-selection pipeline
(apps/live_trader/candidate_selection.py) once and persists the result
(core/market_data/candidate_selection_store.py) for the next trading
day's market-open live_trader run to read instead of repeating all of it
starting at 9 AM.

Run via scripts/run_overnight_selection.sh, well before market open -
NOT scripts/run_live_paper_session.sh, which is the market-open trading
entry point (and will use this script's saved result automatically if
present for today's trading_date, falling back to selecting live
otherwise - see apps/live_trader/main.py's build_live_session).

Never touches a broker - no order placement, no reconciliation, no
LIVE_CONFIRM gate. LIVE_BROKER_PROVIDER/LIVE_MARKET_DATA_PROVIDER are
still read (via the same LiveTraderConfig the live trader uses, so the
exact same provider-combination validation applies), just never acted on
beyond deciding which market-data provider to select against.
"""

from __future__ import annotations

from apps.live_trader.candidate_selection import select_session_instruments
from apps.live_trader.config import (
    AngelOneEnvCredentials,
    GrowwEnvCredentials,
    KiteEnvCredentials,
    load_angelone_credentials_from_env,
    load_groww_credentials_from_env,
    load_kite_credentials_from_env,
    load_live_trader_config_from_env,
)
from apps.live_trader.main import MANDATE_PATH, UNIVERSE_PATH, load_strategy_config
from core.allocations.daily_mandate import load_daily_mandate
from core.market_data.bucket_selector import BucketSelectionResult
from core.market_data.candidate_prescreen import PrescreenResult
from core.market_data.candidate_selection_store import save_candidate_selection
from core.market_data.universe_loader import load_instrument_universe
from core.market_data.universe_selector import TradingUniverseSelector, UniverseSelectionCriteria
from core.notifications.alert_sink import AlertSink, ConsoleAlertSink
from core.portfolio.allocation import build_portfolio_allocation_plan


def select_and_save_candidates(
    mandate_path: str = MANDATE_PATH,
    universe_path: str = UNIVERSE_PATH,
    alert_sink: AlertSink | None = None,
) -> None:
    """
    Run candidate selection for whatever ``trading_date`` is currently
    set in ``mandate_path`` and persist the result.

    ``mandate_path``'s ``trading_date`` should already be set to the day
    this selection is *for* (the next trading day, not "today" at the
    time this runs) - see ``scripts/run_overnight_selection.sh``, which
    sets it before calling this.
    """
    alerts = alert_sink if alert_sink is not None else ConsoleAlertSink()
    live_config = load_live_trader_config_from_env()

    try:
        mandate = load_daily_mandate(mandate_path)
        allocation_plan = build_portfolio_allocation_plan(mandate)
        strategy_name, _strategy_params, candidate_profile = load_strategy_config(
            live_config.strategy_config_path
        )

        universe = load_instrument_universe(universe_path)
        candidate_instruments = TradingUniverseSelector().select(
            universe, UniverseSelectionCriteria(exchange=live_config.universe_exchange)
        )

        groww_credentials: GrowwEnvCredentials | None = None
        if live_config.market_data_provider == "groww":
            groww_credentials = load_groww_credentials_from_env()

        kite_credentials: KiteEnvCredentials | None = None
        if live_config.market_data_provider == "kite":
            kite_credentials = load_kite_credentials_from_env()

        angelone_credentials: AngelOneEnvCredentials | None = None
        if live_config.market_data_provider == "angelone":
            angelone_credentials = load_angelone_credentials_from_env()

        _, prescreen_result, bucket_result = select_session_instruments(
            live_config,
            candidate_instruments,
            mandate,
            allocation_plan,
            groww_credentials,
            kite_credentials,
            angelone_credentials,
            candidate_profile,
        )
    except Exception as exc:
        alerts.send(
            f"overnight candidate selection failed: {type(exc).__name__}: {exc}",
            severity="critical",
        )
        raise

    path = save_candidate_selection(
        allocation_plan.trading_date,
        bucket_result.selected,
        selected_detail=_selected_detail_rows(prescreen_result, bucket_result),
        excluded_detail=_excluded_detail_rows(prescreen_result, bucket_result),
        strategy_name=strategy_name,
    )
    _print_summary(allocation_plan.trading_date, prescreen_result, bucket_result, path)


def _selected_detail_rows(
    prescreen_result: PrescreenResult | None, bucket_result: BucketSelectionResult
) -> list[dict]:
    """
    One row per selected symbol with every number its selection was
    decided from - the "why" the dashboard shows. News fields are
    ``None``/empty for every provider except Angel One (no prescreen
    news stage ran).
    """
    prescreen_by_symbol = prescreen_result.detail_by_symbol if prescreen_result is not None else {}
    rows = []
    for detail in bucket_result.selection_detail:
        pre = prescreen_by_symbol.get(detail.symbol)
        rows.append(
            {
                "symbol": detail.symbol,
                "exchange": detail.exchange,
                "last_close": detail.last_close,
                "volatility_pct": detail.volatility_pct,
                "volatility_ceiling": detail.volatility_ceiling,
                "volatility_source": detail.volatility_source,
                "news_score": pre.news_score if pre is not None else None,
                "headlines": pre.headlines if pre is not None else [],
            }
        )
    return rows


def _excluded_detail_rows(
    prescreen_result: PrescreenResult | None, bucket_result: BucketSelectionResult
) -> list[dict]:
    """Every excluded symbol, tagged with which stage cut it and why - the full "whole info" exclusion picture."""
    rows: list[dict] = []

    if prescreen_result is not None:
        for instrument in prescreen_result.excluded_unaffordable:
            detail = prescreen_result.detail_by_symbol.get(instrument.symbol)
            rows.append(
                {
                    "symbol": instrument.symbol,
                    "exchange": instrument.exchange,
                    "stage": "prescreen",
                    "reason": "unaffordable",
                    "ltp": detail.ltp if detail is not None else None,
                }
            )
        for instrument in prescreen_result.excluded_no_news:
            detail = prescreen_result.detail_by_symbol.get(instrument.symbol)
            rows.append(
                {
                    "symbol": instrument.symbol,
                    "exchange": instrument.exchange,
                    "stage": "prescreen",
                    "reason": "no_qualifying_news",
                    "ltp": detail.ltp if detail is not None else None,
                    "news_score": detail.news_score if detail is not None else None,
                }
            )
        for instrument in prescreen_result.excluded_price_unavailable:
            rows.append(
                {
                    "symbol": instrument.symbol,
                    "exchange": instrument.exchange,
                    "stage": "prescreen",
                    "reason": "no_price_data",
                }
            )

    for exclusion in bucket_result.excluded:
        rows.append(
            {
                "symbol": exclusion.symbol,
                "exchange": exclusion.exchange,
                "stage": "bucket",
                "reason": exclusion.reason,
                "last_close": exclusion.last_close,
                "volatility_pct": exclusion.volatility_pct,
                "volatility_ceiling": exclusion.volatility_ceiling,
            }
        )

    return rows


def _print_summary(
    trading_date: str,
    prescreen_result: PrescreenResult | None,
    bucket_result: BucketSelectionResult,
    path,
) -> None:
    print("=== OVERNIGHT CANDIDATE SELECTION ===")
    print(f"Trading date: {trading_date}")

    if prescreen_result is not None:
        total_screened = (
            len(prescreen_result.survivors)
            + len(prescreen_result.excluded_price_unavailable)
            + len(prescreen_result.excluded_unaffordable)
            + len(prescreen_result.excluded_no_news)
        )
        print(f"Prescreen: screened {total_screened}, survived {len(prescreen_result.survivors)}")
        print(f"  excluded unaffordable: {len(prescreen_result.excluded_unaffordable)}")
        print(f"  excluded no qualifying news: {len(prescreen_result.excluded_no_news)}")
        print(f"  excluded no price data: {len(prescreen_result.excluded_price_unavailable)}")

    print(f"Selected ({len(bucket_result.selected)}): {', '.join(i.symbol for i in bucket_result.selected) or 'none'}")
    if bucket_result.excluded:
        print(f"Excluded from bucket ({len(bucket_result.excluded)}):")
        for exclusion in bucket_result.excluded:
            print(f"  -> {exclusion.exchange}:{exclusion.symbol} reason={exclusion.reason}")

    print(f"Saved to {path}")


def main() -> None:
    select_and_save_candidates()


if __name__ == "__main__":
    main()

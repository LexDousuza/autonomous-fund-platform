from core.backtest.engine import SimpleBacktestEngine
from core.market_data.csv_feed import CSVMarketDataFeed
from core.market_data.service import MarketDataService
from core.strategy.base import StrategyContext
import core.strategy.simple_breakout_momentum  # Registers the strategy
from core.strategy.registry import registry
from core.utils.config_loader import load_yaml_file


def main() -> None:
    market_data = MarketDataService()
    market_data.register_feed(
        "csv_backtest",
        CSVMarketDataFeed("data/backtests/sample_reliance_1m.csv"),
    )
    # No symbols subscribed: stream() replays every bar in the file,
    # matching the previous unfiltered load_bars_from_csv() behaviour.
    bars = list(market_data.stream())

    strategy_cfg = load_yaml_file("config/strategies/simple_breakout_momentum.yaml")
    strategy_name = strategy_cfg["strategy"]["name"]
    strategy_params = strategy_cfg["params"]

    context = StrategyContext(
        strategy_name=strategy_name,
        parameters=strategy_params,
    )

    strategy = registry.create(
        strategy_name,
        context,
    )

    quantity_per_trade = int(strategy_params.get("quantity_per_trade", 5))

    engine = SimpleBacktestEngine(
        initial_cash=100000.0,
        quantity_per_trade=quantity_per_trade,
    )

    result = engine.run(strategy, bars)

    print("=== BACKTEST RESULT ===")
    print(f"Strategy: {strategy_name}")
    print(f"Params: {strategy_params}")
    print(f"Initial cash: {result.initial_cash}")
    print(f"Ending cash: {result.ending_cash}")
    print(f"Total realized_pnl: {result.total_realized_pnl}")
    print(f"Total trades: {result.total_trades}")
    print(f"Closed trades: {result.closed_trades}")
    print(f"Open trades: {result.open_trades}")
    print()

    if result.trades:
        print("=== TRADE LOG ===")
        for i, trade in enumerate(result.trades, start=1):
            print(
                f"{i}. {trade.exchange}:{trade.symbol} | "
                f"entry={trade.entry_time} @ {trade.entry_price} | "
                f"exit={trade.exit_time} @ {trade.exit_price} | "
                f"qty={trade.quantity} | pnl={trade.realized_pnl}"
            )
    else:
        print("No trades generated")


if __name__ == "__main__":
    main()
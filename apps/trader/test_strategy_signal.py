from datetime import datetime, timedelta

from core.strategy.base import MarketBar, StrategyContext
from core.strategy.simple_breakout_momentum import SimpleBreakoutMomentumStrategy


def main() -> None:
    context = StrategyContext(
        strategy_name="simple_breakout_momentum",
        parameters={},
    )

    strategy = SimpleBreakoutMomentumStrategy(context)

    previous_bar = MarketBar(
        symbol="RELIANCE",
        exchange="NSE",
        timestamp=datetime.now() - timedelta(minutes=1),
        open=2480.0,
        high=2495.0,
        low=2475.0,
        close=2490.0,
        volume=100000,
    )

    current_bar = MarketBar(
        symbol="RELIANCE",
        exchange="NSE",
        timestamp=datetime.now(),
        open=2492.0,
        high=2510.0,
        low=2490.0,
        close=2501.0,   # above previous high (2495)
        volume=150000,  # above previous volume (100000)
    )

    signals = strategy.on_bar(
        bar=current_bar,
        history=[previous_bar],
        open_positions={},
    )

    print("Signals generated:", len(signals))
    for signal in signals:
        print(signal)


if __name__ == "__main__":
    main()
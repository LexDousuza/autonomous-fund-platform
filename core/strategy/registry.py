from __future__ import annotations

from typing import Type

from core.strategy.base import Strategy, StrategyContext


class StrategyRegistry:
    """
    Registry for all available trading strategies.
    """

    def __init__(self) -> None:
        self._strategies: dict[str, Type[Strategy]] = {}

    def register(
        self,
        name: str,
        strategy_class: Type[Strategy],
    ) -> None:
        if name in self._strategies:
            raise ValueError(f"Strategy '{name}' is already registered.")

        self._strategies[name] = strategy_class

    def create(
        self,
        name: str,
        context: StrategyContext,
    ) -> Strategy:
        if name not in self._strategies:
            available = ", ".join(sorted(self._strategies))
            raise ValueError(
                f"Unknown strategy '{name}'. "
                f"Available strategies: {available}"
            )

        return self._strategies[name](context)

    def available(self) -> list[str]:
        return sorted(self._strategies.keys())


registry = StrategyRegistry()

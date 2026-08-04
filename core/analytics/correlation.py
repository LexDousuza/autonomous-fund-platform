"""
Pairwise return correlation across symbols - pure statistics, no data
fetching of its own (see core/analytics/daily_bars.py for that).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CorrelationMatrix:
    symbols: list[str]
    matrix: list[list[float | None]]  # matrix[i][j] = corr(symbols[i], symbols[j]); None if not computable


def _daily_returns(closes: list[float]) -> list[float]:
    return [(closes[i] / closes[i - 1]) - 1.0 for i in range(1, len(closes)) if closes[i - 1] != 0]


def _pearson(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[-n:], b[-n:]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    if var_a == 0 or var_b == 0:
        return None
    return cov / (var_a**0.5 * var_b**0.5)


def compute_correlation_matrix(closes_by_symbol: dict[str, list[tuple[str, float]]]) -> CorrelationMatrix:
    """
    ``closes_by_symbol``: ``{symbol: [(date, close), ...]}`` in ascending
    date order (the shape ``core.analytics.daily_bars.load_closes_by_symbol``
    returns). Aligns each pair on however many trailing observations they
    both have (not a strict date-join) - fine for same-exchange equities
    that share a trading calendar, which is all this is used for today.
    """
    symbols = sorted(closes_by_symbol)
    returns = {symbol: _daily_returns([close for _, close in closes_by_symbol[symbol]]) for symbol in symbols}

    # Deliberately no diagonal special-case (no hardcoded 1.0): a
    # zero-variance (flat-price) series' self-correlation is exactly as
    # undefined (0/0) as its correlation with anything else - _pearson
    # already returns None for that, uniformly.
    matrix: list[list[float | None]] = [
        [_pearson(returns[row_symbol], returns[col_symbol]) for col_symbol in symbols]
        for row_symbol in symbols
    ]

    return CorrelationMatrix(symbols=symbols, matrix=matrix)

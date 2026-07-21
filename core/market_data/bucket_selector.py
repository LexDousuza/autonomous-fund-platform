"""
Capital- and risk-appetite-aware stock bucket selection.

``TradingUniverseSelector`` (``universe_selector.py``) narrows the full
instrument universe down to a session's *candidate* instruments using
static filters (exchange/segment/tags). ``CapitalRiskBucketSelector`` is
a second pass over that candidate list: given each candidate's recent
price history and the day's capital/risk-appetite constraints, it decides
which candidates are actually worth trading today.

Two rules, always applied together:

- **Affordability** (hard filter, independent of risk appetite): a
  candidate whose last close exceeds ``max_capital_per_trade`` can never
  be sized into a position at all - ``PositionSizingEngine`` would floor
  it to zero shares - so excluding it here means the session doesn't
  waste a signal, a risk check, or a subscription on it.
- **Volatility ceiling** (driven by ``risk_appetite``): a simple,
  deterministic proxy for how much a symbol moves - the mean daily
  high/low range as a fraction of close, over the supplied price history.
  Tighter for a more conservative appetite; unconstrained for aggressive.

Deliberately a pure function of ``(instruments, price_history, criteria)``
- it does not care whether ``price_history`` came from real historical
data, a live feed's accumulated cache, or synthetic demo bars, so the
same selection logic is exercised (and testable) regardless of which
``MarketDataFeed`` provider a session is using.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.market_data.universe_models import Instrument
from core.strategy.base import MarketBar

RISK_APPETITE_VOLATILITY_CEILINGS: dict[str, float | None] = {
    "conservative": 0.02,
    "moderate": 0.04,
    "aggressive": None,  # no volatility ceiling
}


@dataclass(slots=True)
class BucketSelectionCriteria:
    """
    Inputs that decide which candidates make it into the day's tradable
    bucket. ``max_capital_per_trade`` and ``risk_appetite`` are normally
    read straight from a ``PortfolioAllocationPlan``/``DailyMandate`` -
    kept as plain fields here (rather than depending on those types
    directly) so this module only takes exactly what it needs.
    ``max_bucket_size`` is optional: ``None`` means no cap beyond
    whatever affordability/volatility already excluded.
    """

    risk_appetite: str
    max_capital_per_trade: float
    max_bucket_size: int | None = None


@dataclass(slots=True)
class BucketExclusion:
    """One candidate that did not make it into the bucket, and why."""

    symbol: str
    exchange: str
    reason: str


@dataclass(slots=True)
class BucketSelectionResult:
    selected: list[Instrument] = field(default_factory=list)
    excluded: list[BucketExclusion] = field(default_factory=list)


def _average_daily_range_pct(bars: list[MarketBar]) -> float | None:
    """Mean of (high - low) / close across bars with a positive close, or None if none qualify."""
    ranges = [(bar.high - bar.low) / bar.close for bar in bars if bar.close > 0]
    if not ranges:
        return None
    return sum(ranges) / len(ranges)


class CapitalRiskBucketSelector:
    """
    Deterministic: for the same ``(instruments, price_history, criteria)``
    input, always returns the same selection in the same order (source
    universe order, matching ``TradingUniverseSelector``'s convention).
    """

    def select(
        self,
        instruments: list[Instrument],
        price_history: dict[str, list[MarketBar]],
        criteria: BucketSelectionCriteria,
    ) -> BucketSelectionResult:
        if criteria.risk_appetite not in RISK_APPETITE_VOLATILITY_CEILINGS:
            raise ValueError(
                f"Unknown risk_appetite '{criteria.risk_appetite}'. "
                f"Supported: {sorted(RISK_APPETITE_VOLATILITY_CEILINGS)}"
            )
        volatility_ceiling = RISK_APPETITE_VOLATILITY_CEILINGS[criteria.risk_appetite]

        result = BucketSelectionResult()

        for instrument in instruments:
            bars = price_history.get(instrument.symbol) or []

            if not bars:
                result.excluded.append(
                    BucketExclusion(
                        symbol=instrument.symbol,
                        exchange=instrument.exchange,
                        reason="no_price_history",
                    )
                )
                continue

            last_close = bars[-1].close
            if last_close <= 0:
                result.excluded.append(
                    BucketExclusion(
                        symbol=instrument.symbol,
                        exchange=instrument.exchange,
                        reason="invalid_price",
                    )
                )
                continue

            if last_close > criteria.max_capital_per_trade:
                result.excluded.append(
                    BucketExclusion(
                        symbol=instrument.symbol,
                        exchange=instrument.exchange,
                        reason="unaffordable_at_current_capital",
                    )
                )
                continue

            avg_range_pct = _average_daily_range_pct(bars)
            if (
                volatility_ceiling is not None
                and avg_range_pct is not None
                and avg_range_pct > volatility_ceiling
            ):
                result.excluded.append(
                    BucketExclusion(
                        symbol=instrument.symbol,
                        exchange=instrument.exchange,
                        reason="volatility_exceeds_risk_appetite",
                    )
                )
                continue

            result.selected.append(instrument)

        if criteria.max_bucket_size is not None and len(result.selected) > criteria.max_bucket_size:
            overflow = result.selected[criteria.max_bucket_size :]
            result.selected = result.selected[: criteria.max_bucket_size]
            result.excluded.extend(
                BucketExclusion(
                    symbol=instrument.symbol,
                    exchange=instrument.exchange,
                    reason="bucket_size_cap",
                )
                for instrument in overflow
            )

        return result


def select_bucket(
    instruments: list[Instrument],
    price_history: dict[str, list[MarketBar]],
    criteria: BucketSelectionCriteria,
) -> BucketSelectionResult:
    """Convenience wrapper around ``CapitalRiskBucketSelector().select(...)``."""
    return CapitalRiskBucketSelector().select(instruments, price_history, criteria)

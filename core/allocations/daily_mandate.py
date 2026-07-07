from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class DailyCapitalBuckets:
    reserve_bucket: float
    intraday_bucket: float

    @property
    def total(self) -> float:
        return self.reserve_bucket + self.intraday_bucket


@dataclass(slots=True)
class DailyRiskLimits:
    max_daily_loss: float
    max_trades_per_day: int
    stop_new_entries_after: str | None = None
    force_square_off_at: str | None = None


@dataclass(slots=True)
class DailyMandate:
    trading_date: date
    buckets: DailyCapitalBuckets
    risk_limits: DailyRiskLimits
    execution_mode: str = "paper"  # "paper" or "live"

    def validate(self) -> None:
        if self.buckets.reserve_bucket < 0:
            raise ValueError("reserve_bucket cannot be negative")
        if self.buckets.intraday_bucket < 0:
            raise ValueError("intraday_bucket cannot be negative")
        if self.risk_limits.max_daily_loss < 0:
            raise ValueError("max_daily_loss cannot be negative")
        if self.risk_limits.max_trades_per_day <= 0:
            raise ValueError("max_trades_per_day must be greater than zero")
        if self.execution_mode not in {"paper", "live"}:
            raise ValueError("execution_mode must be either 'paper' or 'live'")


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _load_yaml(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Mandate file not found: {file_path}")

    with file_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError("Mandate YAML must contain a top-level mapping/object")

    return data


def load_daily_mandate(path: str | Path) -> DailyMandate:
    raw = _load_yaml(path)

    try:
        trading_date = _parse_date(raw["trading_date"])

        bucket_raw = raw["buckets"]
        risk_raw = raw["risk_limits"]

        buckets = DailyCapitalBuckets(
            reserve_bucket=float(bucket_raw["reserve_bucket"]),
            intraday_bucket=float(bucket_raw["intraday_bucket"]),
        )

        risk_limits = DailyRiskLimits(
            max_daily_loss=float(risk_raw["max_daily_loss"]),
            max_trades_per_day=int(risk_raw["max_trades_per_day"]),
            stop_new_entries_after=risk_raw.get("stop_new_entries_after"),
            force_square_off_at=risk_raw.get("force_square_off_at"),
        )

        mandate = DailyMandate(
            trading_date=trading_date,
            buckets=buckets,
            risk_limits=risk_limits,
            execution_mode=raw.get("execution_mode", "paper"),
        )
        mandate.validate()
        return mandate

    except KeyError as exc:
        raise ValueError(f"Missing required mandate field: {exc}") from exc
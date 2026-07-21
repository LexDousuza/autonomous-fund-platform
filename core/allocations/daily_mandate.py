from __future__ import annotations

import re
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


RISK_APPETITES = {"conservative", "moderate", "aggressive"}


@dataclass(slots=True)
class DailyMandate:
    trading_date: date
    buckets: DailyCapitalBuckets
    risk_limits: DailyRiskLimits
    execution_mode: str = "paper"  # "paper" or "live"
    risk_appetite: str = "moderate"  # "conservative", "moderate", or "aggressive"

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
        if self.risk_appetite not in RISK_APPETITES:
            raise ValueError(f"risk_appetite must be one of {sorted(RISK_APPETITES)}")


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
            risk_appetite=raw.get("risk_appetite", "moderate"),
        )
        mandate.validate()
        return mandate

    except KeyError as exc:
        raise ValueError(f"Missing required mandate field: {exc}") from exc


# field name -> whether its YAML value is quoted (strings/times) or bare
# (numbers). Deliberately excludes trading_date (see
# scripts/set_mandate_trading_date.py, which owns that field on its own
# daily cadence) and execution_mode (a live/paper switch too consequential
# to change via a casual dashboard form).
EDITABLE_MANDATE_FIELDS: dict[str, bool] = {
    "reserve_bucket": False,
    "intraday_bucket": False,
    "max_daily_loss": False,
    "max_trades_per_day": False,
    "stop_new_entries_after": True,
    "force_square_off_at": True,
    "risk_appetite": True,
}


def update_daily_mandate_fields(path: str | Path, updates: dict[str, str]) -> None:
    """
    Update one or more fields in ``EDITABLE_MANDATE_FIELDS`` in a daily
    mandate YAML file via targeted per-line text replacement, not a full
    YAML load/dump - preserves the file's comments and formatting (a
    full ``yaml.safe_dump`` round-trip silently drops comments; see
    ``scripts/set_mandate_trading_date.py`` for the same approach
    applied to ``trading_date`` specifically). Field names are unique
    enough in this file's shape that a line-anchored regex finds the
    right line regardless of its nesting under ``buckets``/``risk_limits``.

    Raises ``ValueError`` for an unknown field name or one not found in
    the file (e.g. the file's shape changed) - never partially writes on
    a failure part-way through multiple updates.
    """
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")

    for field, new_value in updates.items():
        if field not in EDITABLE_MANDATE_FIELDS:
            raise ValueError(f"'{field}' is not an editable mandate field")

        quoted = EDITABLE_MANDATE_FIELDS[field]
        value_str = f'"{new_value}"' if quoted else str(new_value)
        pattern = re.compile(rf"^(\s*{re.escape(field)}:\s*).*$", re.MULTILINE)

        text, count = pattern.subn(lambda m: m.group(1) + value_str, text, count=1)
        if count == 0:
            raise ValueError(f"Could not find field '{field}' in {file_path}")

    file_path.write_text(text, encoding="utf-8")
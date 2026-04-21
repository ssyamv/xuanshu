from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

NormalizedStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TradeCountSufficiency(StrEnum):
    INSUFFICIENT = "insufficient"
    SUFFICIENT = "sufficient"


class OverfitRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RegimeFit(StrEnum):
    ALIGNED = "aligned"
    MISALIGNED = "misaligned"
    UNKNOWN = "unknown"


class BacktestDatasetRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    regime_fit: RegimeFit

    @field_validator("start", "end")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dataset_range timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_range_order(self) -> "BacktestDatasetRange":
        if self.end < self.start:
            raise ValueError("dataset_range.end must be >= dataset_range.start")
        return self


class BacktestReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backtest_report_id: NormalizedStr
    strategy_package_id: NormalizedStr
    strategy_def_id: NormalizedStr
    symbol_scope: list[NormalizedStr] = Field(min_length=1)
    dataset_range: BacktestDatasetRange
    sample_count: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    trade_count_sufficiency: TradeCountSufficiency
    net_pnl: float
    return_percent: float
    max_drawdown: float = Field(ge=0.0)
    win_rate: float = Field(ge=0.0, le=1.0)
    profit_factor: float = Field(ge=0.0)
    stability_score: float = Field(ge=0.0, le=1.0)
    overfit_risk: OverfitRisk
    failure_modes: list[NormalizedStr]
    invalidating_conditions: list[NormalizedStr]
    generated_at: datetime

    @field_validator(
        "net_pnl",
        "return_percent",
        "max_drawdown",
        "win_rate",
        "profit_factor",
        "stability_score",
    )
    @classmethod
    def validate_finite_float(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("float fields must be finite")
        return value

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(UTC)

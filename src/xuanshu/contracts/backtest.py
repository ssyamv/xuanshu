from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NormalizedStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class BacktestReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backtest_report_id: NormalizedStr
    strategy_package_id: NormalizedStr
    symbol_scope: list[NormalizedStr] = Field(min_length=1)
    dataset_range: dict[str, object]
    sample_count: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    net_pnl: float
    max_drawdown: float = Field(ge=0.0)
    win_rate: float = Field(ge=0.0, le=1.0)
    profit_factor: float = Field(ge=0.0)
    stability_score: float = Field(ge=0.0, le=1.0)
    overfit_risk: NormalizedStr
    failure_modes: list[NormalizedStr]
    invalidating_conditions: list[NormalizedStr]
    generated_at: datetime

    @field_validator(
        "net_pnl",
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

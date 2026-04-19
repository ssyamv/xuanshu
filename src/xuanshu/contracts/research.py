from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NormalizedStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ResearchTrigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"
    EVENT = "event"


class StrategyPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_package_id: NormalizedStr
    generated_at: datetime
    trigger: ResearchTrigger
    symbol_scope: list[NormalizedStr] = Field(min_length=1)
    market_environment_scope: list[NormalizedStr] = Field(min_length=1)
    strategy_family: NormalizedStr
    directionality: NormalizedStr
    entry_rules: dict[str, object]
    exit_rules: dict[str, object]
    position_sizing_rules: dict[str, object]
    risk_constraints: dict[str, object]
    parameter_set: dict[str, object]
    backtest_summary: dict[str, object]
    performance_summary: dict[str, object]
    failure_modes: list[NormalizedStr]
    invalidating_conditions: list[NormalizedStr]
    research_reason: NormalizedStr

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(UTC)

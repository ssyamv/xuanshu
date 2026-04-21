from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from xuanshu.contracts.strategy_definition import StrategyDefinition

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
    strategy_definition: StrategyDefinition | None = None
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
    score: float = Field(default=0.0, ge=0.0)
    score_basis: NormalizedStr = "backtest_return_percent"

    @model_validator(mode="after")
    def validate_embedded_strategy_definition(self) -> "StrategyPackage":
        if self.strategy_definition is None and (
            self._uses_strategy_dsl(self.entry_rules) or self._uses_strategy_dsl(self.exit_rules)
        ):
            raise ValueError("strategy_definition is required")
        return self

    @staticmethod
    def _uses_strategy_dsl(rules: dict[str, object]) -> bool:
        return "all" in rules or "any" in rules

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        return _normalize_timezone_aware_timestamp(value, field_name="generated_at")


def _normalize_timezone_aware_timestamp(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)

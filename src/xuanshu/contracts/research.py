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
    strategy_definition: StrategyDefinition
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
    score: float = Field(ge=0.0)
    score_basis: NormalizedStr


    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        return _normalize_timezone_aware_timestamp(value, field_name="generated_at")

    @model_validator(mode="after")
    def validate_strategy_definition_consistency(self) -> "StrategyPackage":
        definition = self.strategy_definition
        if self.symbol_scope[0] != definition.symbol:
            raise ValueError("symbol_scope[0] must match strategy_definition.symbol")
        if self.strategy_family != definition.strategy_family:
            raise ValueError("strategy_family must match strategy_definition.strategy_family")
        if self.directionality != definition.directionality:
            raise ValueError("directionality must match strategy_definition.directionality")
        if self.entry_rules != definition.entry_rules:
            raise ValueError("entry_rules must match strategy_definition.entry_rules")
        if self.exit_rules != definition.exit_rules:
            raise ValueError("exit_rules must match strategy_definition.exit_rules")
        if self.position_sizing_rules != definition.position_sizing_rules:
            raise ValueError("position_sizing_rules must match strategy_definition.position_sizing_rules")
        if self.risk_constraints != definition.risk_constraints:
            raise ValueError("risk_constraints must match strategy_definition.risk_constraints")
        if self.score != definition.score:
            raise ValueError("score must match strategy_definition.score")
        if self.score_basis != definition.score_basis:
            raise ValueError("score_basis must match strategy_definition.score_basis")
        if self.parameter_set != definition.parameter_set:
            raise ValueError("parameter_set must match strategy_definition.parameter_set")
        return self


def _normalize_timezone_aware_timestamp(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)

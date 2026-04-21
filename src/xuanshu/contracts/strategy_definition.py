from __future__ import annotations

from collections.abc import Mapping
from numbers import Real
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

NormalizedStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_SUPPORTED_INDICATORS = {"sma", "ema", "atr", "highest", "lowest", "zscore"}
_SUPPORTED_SOURCES = {"open", "high", "low", "close", "volume"}
_SUPPORTED_OPERATORS = {
    "greater_than",
    "less_than",
    "crosses_above",
    "crosses_below",
    "take_profit_bps",
    "stop_loss_bps",
    "time_stop_minutes",
}
_SUPPORTED_DIRECTIONALITY = {"long_only", "short_only"}
_SUPPORTED_SCORE_BASES = {"backtest_return_percent"}


class IndicatorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    source: str | None = None
    window: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _SUPPORTED_INDICATORS:
            raise ValueError("unsupported indicator")
        return normalized

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip().lower()
        if normalized not in _SUPPORTED_SOURCES:
            raise ValueError("unsupported source")
        return normalized


class StrategyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_def_id: NormalizedStr
    symbol: NormalizedStr
    strategy_family: NormalizedStr
    directionality: str
    feature_spec: dict[str, object]
    entry_rules: dict[str, object]
    exit_rules: dict[str, object]
    position_sizing_rules: dict[str, object]
    risk_constraints: dict[str, object]
    parameter_set: dict[str, object]
    score: float = Field(ge=0.0)
    score_basis: str

    @field_validator("directionality")
    @classmethod
    def validate_directionality(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _SUPPORTED_DIRECTIONALITY:
            raise ValueError("unsupported directionality")
        return normalized

    @field_validator("score_basis")
    @classmethod
    def validate_score_basis(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _SUPPORTED_SCORE_BASES:
            raise ValueError("unsupported score basis")
        return normalized

    @model_validator(mode="after")
    def validate_supported_rule_tree(self) -> "StrategyDefinition":
        self._validate_rule_tree(self.entry_rules)
        self._validate_rule_tree(self.exit_rules)
        indicators = self.feature_spec.get("indicators", [])
        if not isinstance(indicators, list) or not indicators:
            raise ValueError("feature_spec.indicators must not be empty")
        for indicator in indicators:
            IndicatorSpec.model_validate(indicator)
        return self

    @classmethod
    def _validate_rule_tree(cls, node: object) -> None:
        if isinstance(node, dict):
            if "all" in node or "any" in node:
                key = "all" if "all" in node else "any"
                children = node[key]
                if not isinstance(children, list) or not children:
                    raise ValueError(f"{key} must contain rule nodes")
                for child in children:
                    cls._validate_rule_tree(child)
                return
            op = node.get("op")
            if not isinstance(op, str) or op.strip().lower() not in _SUPPORTED_OPERATORS:
                raise ValueError("unsupported operator")
            normalized_op = op.strip().lower()
            if normalized_op in {"greater_than", "less_than", "crosses_above", "crosses_below"}:
                cls._validate_comparison_rule(node)
            elif normalized_op in {"take_profit_bps", "stop_loss_bps", "time_stop_minutes"}:
                cls._validate_positive_value_rule(node, op=normalized_op)
            return
        raise ValueError("rule node must be a mapping")

    @staticmethod
    def _validate_comparison_rule(node: Mapping[str, object]) -> None:
        if "left" not in node or "right" not in node:
            raise ValueError("left and right are required")
        left = node["left"]
        right = node["right"]
        if not isinstance(left, str) or not left.strip():
            raise ValueError("left must be a non-empty string")
        if isinstance(right, str):
            if not right.strip():
                raise ValueError("right must be a non-empty string or const mapping")
            return
        if isinstance(right, Mapping):
            if set(right.keys()) != {"const"}:
                raise ValueError("right must be a non-empty string or const mapping")
            const_value = right["const"]
            if not isinstance(const_value, Real) or isinstance(const_value, bool):
                raise ValueError("right const must be numeric")
            return
        raise ValueError("right must be a non-empty string or const mapping")

    @staticmethod
    def _validate_positive_value_rule(node: Mapping[str, object], *, op: str) -> None:
        value = node.get("value")
        if not isinstance(value, Real) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{op} value must be positive")

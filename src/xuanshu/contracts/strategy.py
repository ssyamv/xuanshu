from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from xuanshu.core.enums import ApprovalState, RunMode


class ApprovedStrategyBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_def_id: str = Field(min_length=1)
    strategy_package_id: str = Field(min_length=1)
    backtest_report_id: str = Field(min_length=1)
    score: float = Field(ge=0.0)
    score_basis: str = Field(min_length=1)
    approval_record_id: str = Field(min_length=1)
    activated_at: datetime

    @field_validator("activated_at")
    @classmethod
    def validate_activated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("activated_at must be timezone-aware")
        return value.astimezone(UTC)


class StrategyConfigSnapshot(BaseModel):
    version_id: str = Field(min_length=1)
    generated_at: datetime
    effective_from: datetime
    expires_at: datetime
    symbol_whitelist: list[str] = Field(min_length=1)
    strategy_enable_flags: dict[str, bool]
    risk_multiplier: float = Field(ge=0.0, le=1.0)
    per_symbol_max_position: float = Field(ge=0.0, le=1.0)
    max_leverage: int = Field(ge=1, le=3)
    market_mode: RunMode
    approval_state: ApprovalState
    source_reason: str = Field(min_length=1)
    ttl_sec: int = Field(gt=0)
    symbol_strategy_bindings: dict[str, ApprovedStrategyBinding] = Field(default_factory=dict)

    def is_effective(self, reference_time: datetime) -> bool:
        reference_time = self._normalize_reference_time(reference_time)
        return reference_time >= self.effective_from

    def is_expired(self, reference_time: datetime) -> bool:
        reference_time = self._normalize_reference_time(reference_time)
        return reference_time >= self.expires_at

    def is_active(self, reference_time: datetime) -> bool:
        reference_time = self._normalize_reference_time(reference_time)
        return (
            self.approval_state == ApprovalState.APPROVED
            and self.is_effective(reference_time)
            and not self.is_expired(reference_time)
        )

    def allows_symbol(self, symbol: str) -> bool:
        return symbol in self.symbol_whitelist

    def is_strategy_enabled(self, strategy_id: str) -> bool:
        return self.strategy_enable_flags.get(strategy_id, False)

    @model_validator(mode="after")
    def validate_temporal_window(self) -> "StrategyConfigSnapshot":
        if self.expires_at <= self.effective_from:
            raise ValueError("expires_at must be after effective_from")
        return self

    @field_validator("generated_at", "effective_from", "expires_at")
    @classmethod
    def validate_timezone_aware_datetimes(cls, value: datetime, info: ValidationInfo) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("symbol_whitelist")
    @classmethod
    def validate_symbol_whitelist(cls, value: list[str]) -> list[str]:
        if _has_blank_symbol_entries(value):
            raise ValueError("symbol_whitelist must not contain blank symbols")
        return value

    @staticmethod
    def _normalize_reference_time(reference_time: datetime) -> datetime:
        if reference_time.tzinfo is None or reference_time.utcoffset() is None:
            raise ValueError("reference_time must be timezone-aware")
        return reference_time.astimezone(UTC)


def _has_blank_symbol_entries(symbols: list[str]) -> bool:
    return any(not symbol.strip() for symbol in symbols)

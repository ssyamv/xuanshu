from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from xuanshu.core.enums import ApprovalState, RunMode


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

    def is_effective(self, reference_time: datetime) -> bool:
        return reference_time >= self.effective_from

    def is_expired(self, reference_time: datetime) -> bool:
        return reference_time >= self.expires_at

    def is_active(self, reference_time: datetime) -> bool:
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

    @field_validator("symbol_whitelist")
    @classmethod
    def validate_symbol_whitelist(cls, value: list[str]) -> list[str]:
        if any(not symbol.strip() for symbol in value):
            raise ValueError("symbol_whitelist must not contain blank symbols")
        return value

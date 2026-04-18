from datetime import datetime

from pydantic import BaseModel, Field

from xuanshu.core.enums import RunMode


class StrategyConfigSnapshot(BaseModel):
    version_id: str
    generated_at: datetime
    effective_from: datetime
    expires_at: datetime
    symbol_whitelist: list[str]
    strategy_enable_flags: dict[str, bool]
    risk_multiplier: float = Field(ge=0.0, le=1.0)
    per_symbol_max_position: float = Field(ge=0.0, le=1.0)
    max_leverage: int = Field(ge=1, le=3)
    market_mode: RunMode
    approval_state: str
    source_reason: str
    ttl_sec: int = Field(gt=0)

    def is_expired(self, reference_time: datetime) -> bool:
        return reference_time >= self.expires_at

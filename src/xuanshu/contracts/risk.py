from datetime import datetime

from pydantic import BaseModel, Field

from xuanshu.core.enums import EntryType, OrderSide, RunMode, SignalUrgency, StrategyId


class CandidateSignal(BaseModel):
    symbol: str = Field(min_length=1)
    strategy_id: StrategyId
    side: OrderSide
    entry_type: EntryType
    urgency: SignalUrgency
    confidence: float = Field(ge=0.0, le=1.0)
    max_hold_ms: int = Field(gt=0)
    cancel_after_ms: int = Field(gt=0)
    risk_tag: str = Field(min_length=1)


class RiskDecision(BaseModel):
    decision_id: str = Field(min_length=1)
    generated_at: datetime
    symbol: str = Field(min_length=1)
    allow_open: bool
    allow_close: bool
    max_position: float = Field(ge=0.0)
    max_order_size: float = Field(ge=0.0)
    risk_mode: RunMode
    reason_codes: list[str]

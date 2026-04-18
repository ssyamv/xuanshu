from datetime import datetime

from pydantic import BaseModel

from xuanshu.core.enums import RunMode


class CandidateSignal(BaseModel):
    symbol: str
    strategy_id: str
    side: str
    entry_type: str
    urgency: str
    confidence: float
    max_hold_ms: int
    cancel_after_ms: int
    risk_tag: str


class RiskDecision(BaseModel):
    decision_id: str
    generated_at: datetime
    symbol: str
    allow_open: bool
    allow_close: bool
    max_position: float
    max_order_size: float
    risk_mode: RunMode
    reason_codes: list[str]

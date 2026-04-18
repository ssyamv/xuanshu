from datetime import datetime

from pydantic import BaseModel, Field


class ExpertOpinion(BaseModel):
    opinion_id: str
    expert_type: str
    generated_at: datetime
    symbol_scope: list[str]
    decision: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_facts: list[str]
    risk_flags: list[str]
    ttl_sec: int = Field(gt=0)

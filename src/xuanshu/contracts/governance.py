from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ExpertOpinion(BaseModel):
    opinion_id: str = Field(min_length=1)
    expert_type: str = Field(min_length=1)
    generated_at: datetime
    symbol_scope: list[str] = Field(min_length=1)
    decision: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_facts: list[str]
    risk_flags: list[str]
    ttl_sec: int = Field(gt=0)

    @field_validator("symbol_scope")
    @classmethod
    def validate_symbol_scope(cls, value: list[str]) -> list[str]:
        if any(not symbol.strip() for symbol in value):
            raise ValueError("symbol_scope must not contain blank symbols")
        return value

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

NormalizedStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    APPROVED_WITH_GUARDRAILS = "approved_with_guardrails"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_record_id: NormalizedStr
    strategy_package_id: NormalizedStr
    backtest_report_id: NormalizedStr
    decision: ApprovalDecision
    decision_reason: NormalizedStr
    guardrails: dict[str, object]
    reviewed_by: NormalizedStr
    review_source: NormalizedStr
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from xuanshu.core.enums import RunMode


class ExecutionCheckpoint(BaseModel):
    checkpoint_id: str
    created_at: datetime
    active_snapshot_version: str
    current_mode: RunMode
    positions_snapshot: dict[str, float]
    open_orders_snapshot: list[dict[str, Any]]
    budget_state: dict[str, int]
    last_public_stream_marker: str | None
    last_private_stream_marker: str | None
    needs_reconcile: bool

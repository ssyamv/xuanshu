from datetime import datetime

from pydantic import BaseModel, Field

from xuanshu.core.enums import OrderSide, RunMode


class CheckpointPosition(BaseModel):
    symbol: str = Field(min_length=1)
    net_quantity: float
    mark_price: float = Field(ge=0.0)
    unrealized_pnl: float


class CheckpointOrder(BaseModel):
    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: OrderSide
    price: float = Field(ge=0.0)
    size: float = Field(gt=0.0)
    status: str = Field(min_length=1)


class CheckpointBudgetState(BaseModel):
    max_daily_loss: float = Field(ge=0.0)
    remaining_daily_loss: float = Field(ge=0.0)
    remaining_notional: float = Field(ge=0.0)
    remaining_order_count: int = Field(ge=0)


class ExecutionCheckpoint(BaseModel):
    checkpoint_id: str = Field(min_length=1)
    created_at: datetime
    active_snapshot_version: str = Field(min_length=1)
    current_mode: RunMode
    positions_snapshot: list[CheckpointPosition] = Field(default_factory=list)
    open_orders_snapshot: list[CheckpointOrder] = Field(default_factory=list)
    budget_state: CheckpointBudgetState
    last_public_stream_marker: str | None = Field(default=None, min_length=1)
    last_private_stream_marker: str | None = Field(default=None, min_length=1)
    needs_reconcile: bool

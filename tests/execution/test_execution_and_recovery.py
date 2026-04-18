from datetime import UTC, datetime

from xuanshu.checkpoints.service import CheckpointService
from xuanshu.contracts.checkpoint import CheckpointBudgetState, ExecutionCheckpoint
from xuanshu.core.enums import RunMode
from xuanshu.execution.engine import build_client_order_id


def test_execution_ids_and_recovery_guard_are_deterministic() -> None:
    checkpoint = ExecutionCheckpoint(
        checkpoint_id="cp-001",
        created_at=datetime.now(UTC),
        active_snapshot_version="snap-001",
        current_mode=RunMode.NORMAL,
        positions_snapshot=[],
        open_orders_snapshot=[],
        budget_state=CheckpointBudgetState(
            max_daily_loss=100.0,
            remaining_daily_loss=25.0,
            remaining_notional=50.0,
            remaining_order_count=10,
        ),
        last_public_stream_marker="pub-1",
        last_private_stream_marker="pri-1",
        needs_reconcile=True,
    )

    assert build_client_order_id("BTC-USDT-SWAP", "breakout", 7) == "BTC-USDT-SWAP-breakout-000007"
    assert CheckpointService().can_open_new_risk(checkpoint) is False

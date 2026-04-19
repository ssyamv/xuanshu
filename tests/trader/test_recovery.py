from datetime import UTC, datetime

import pytest

from xuanshu.contracts.checkpoint import CheckpointBudgetState, ExecutionCheckpoint
from xuanshu.core.enums import RunMode
from xuanshu.trader.recovery import RecoverySupervisor


class _FakeRestClient:
    async def fetch_open_orders(self, symbol: str) -> list[dict[str, object]]:
        return [{"order_id": "ord-1", "symbol": symbol}]

    async def fetch_positions(self, symbol: str) -> list[dict[str, object]]:
        return [{"symbol": symbol, "net_quantity": 1.0}]

    async def fetch_account_summary(self) -> dict[str, object]:
        return {"equity": 1000.0, "available_balance": 800.0}


@pytest.mark.asyncio
async def test_recovery_supervisor_blocks_when_checkpoint_and_exchange_diverge() -> None:
    checkpoint = ExecutionCheckpoint(
        checkpoint_id="cp-1",
        created_at=datetime.now(UTC),
        active_snapshot_version="snap-1",
        current_mode=RunMode.NORMAL,
        positions_snapshot=[],
        open_orders_snapshot=[],
        budget_state=CheckpointBudgetState(
            max_daily_loss=100.0,
            remaining_daily_loss=80.0,
            remaining_notional=60.0,
            remaining_order_count=10,
        ),
        last_public_stream_marker="pub-1",
        last_private_stream_marker="pri-1",
        needs_reconcile=False,
    )

    supervisor = RecoverySupervisor(rest_client=_FakeRestClient())
    result = await supervisor.run_startup_recovery("BTC-USDT-SWAP", checkpoint)

    assert result["run_mode"] == "halted"
    assert result["needs_reconcile"] is True
    assert result["reason"] == "exchange_state_mismatch"

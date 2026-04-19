from datetime import UTC, datetime

import pytest

from xuanshu.contracts.checkpoint import CheckpointBudgetState, ExecutionCheckpoint
from xuanshu.core.enums import RunMode
from xuanshu.trader.recovery import RecoverySupervisor


class _FakeRestClient:
    def __init__(self) -> None:
        self.open_orders_calls: list[tuple[str, str]] = []
        self.positions_calls: list[tuple[str, str]] = []
        self.account_summary_calls: list[str] = []

    async def fetch_open_orders(self, symbol: str, timestamp: str) -> list[dict[str, object]]:
        self.open_orders_calls.append((symbol, timestamp))
        return [{"order_id": "ord-1", "symbol": symbol}]

    async def fetch_positions(self, symbol: str, timestamp: str) -> list[dict[str, object]]:
        self.positions_calls.append((symbol, timestamp))
        return [{"symbol": symbol, "net_quantity": 1.0}]

    async def fetch_account_summary(self, timestamp: str) -> list[dict[str, object]]:
        self.account_summary_calls.append(timestamp)
        return [{"totalEq": "1000.0", "availEq": "800.0"}]


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

    rest_client = _FakeRestClient()
    supervisor = RecoverySupervisor(rest_client=rest_client)
    result = await supervisor.run_startup_recovery(
        "BTC-USDT-SWAP",
        checkpoint,
        timestamp="2026-04-19T00:00:00.000Z",
    )

    assert result["run_mode"] == "halted"
    assert result["needs_reconcile"] is True
    assert result["reason"] == "exchange_state_mismatch"
    assert rest_client.open_orders_calls == [("BTC-USDT-SWAP", "2026-04-19T00:00:00.000Z")]
    assert rest_client.positions_calls == [("BTC-USDT-SWAP", "2026-04-19T00:00:00.000Z")]
    assert rest_client.account_summary_calls == ["2026-04-19T00:00:00.000Z"]

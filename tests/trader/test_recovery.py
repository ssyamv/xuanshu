from datetime import UTC, datetime

import pytest

from xuanshu.contracts.checkpoint import (
    CheckpointBudgetState,
    CheckpointOrder,
    CheckpointPosition,
    ExecutionCheckpoint,
)
from xuanshu.core.enums import RunMode
from xuanshu.trader.recovery import RecoverySupervisor


class _FakeRestClient:
    def __init__(
        self,
        *,
        open_orders: list[dict[str, object]] | None = None,
        positions: list[dict[str, object]] | None = None,
    ) -> None:
        self.open_orders_calls: list[tuple[str, str]] = []
        self.positions_calls: list[tuple[str, str]] = []
        self.account_summary_calls: list[str] = []
        self._open_orders = open_orders or [{"order_id": "ord-1", "symbol": "BTC-USDT-SWAP"}]
        self._positions = positions or [{"symbol": "BTC-USDT-SWAP", "net_quantity": 1.0}]

    async def fetch_open_orders(self, symbol: str, timestamp: str) -> list[dict[str, object]]:
        self.open_orders_calls.append((symbol, timestamp))
        return self._open_orders

    async def fetch_positions(self, symbol: str, timestamp: str) -> list[dict[str, object]]:
        self.positions_calls.append((symbol, timestamp))
        return self._positions

    async def fetch_account_summary(self, timestamp: str) -> list[dict[str, object]]:
        self.account_summary_calls.append(timestamp)
        return [{"totalEq": "1000.0", "availEq": "800.0"}]


def _build_checkpoint(
    *,
    positions_snapshot: list[CheckpointPosition] | None = None,
    open_orders_snapshot: list[CheckpointOrder] | None = None,
) -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        checkpoint_id="cp-1",
        created_at=datetime.now(UTC),
        active_snapshot_version="snap-1",
        current_mode=RunMode.NORMAL,
        positions_snapshot=positions_snapshot or [],
        open_orders_snapshot=open_orders_snapshot or [],
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


@pytest.mark.asyncio
async def test_recovery_supervisor_allows_matching_checkpoint_and_exchange_state() -> None:
    checkpoint = _build_checkpoint(
        positions_snapshot=[
            CheckpointPosition(
                symbol="BTC-USDT-SWAP",
                net_quantity=1.0,
                mark_price=102.5,
                unrealized_pnl=3.0,
            )
        ],
        open_orders_snapshot=[
            CheckpointOrder(
                order_id="ord-1",
                symbol="BTC-USDT-SWAP",
                side="buy",
                price=100.0,
                size=1.0,
                status="live",
            )
        ],
    )
    rest_client = _FakeRestClient(
        open_orders=[
            {
                "order_id": "ord-1",
                "symbol": "BTC-USDT-SWAP",
                "side": "buy",
                "price": 100.0,
                "size": 1.0,
                "status": "live",
            }
        ],
        positions=[
            {
                "symbol": "BTC-USDT-SWAP",
                "net_quantity": 1.0,
                "mark_price": 102.5,
                "unrealized_pnl": 3.0,
            }
        ],
    )
    supervisor = RecoverySupervisor(rest_client=rest_client)

    result = await supervisor.run_startup_recovery(
        "BTC-USDT-SWAP",
        checkpoint,
        timestamp="2026-04-19T00:00:00.000Z",
    )

    assert result["run_mode"] == "normal"
    assert result["needs_reconcile"] is False
    assert result["reason"] == "checkpoint_matches_exchange"
    assert rest_client.account_summary_calls == []


@pytest.mark.asyncio
async def test_recovery_supervisor_blocks_on_equal_count_but_different_content() -> None:
    checkpoint = _build_checkpoint(
        positions_snapshot=[
            CheckpointPosition(
                symbol="BTC-USDT-SWAP",
                net_quantity=1.0,
                mark_price=102.5,
                unrealized_pnl=3.0,
            )
        ],
        open_orders_snapshot=[
            CheckpointOrder(
                order_id="ord-1",
                symbol="BTC-USDT-SWAP",
                side="buy",
                price=100.0,
                size=1.0,
                status="live",
            )
        ],
    )
    rest_client = _FakeRestClient(
        open_orders=[
            {
                "order_id": "ord-1",
                "symbol": "BTC-USDT-SWAP",
                "side": "sell",
                "price": 100.0,
                "size": 1.0,
                "status": "live",
            }
        ],
        positions=[
            {
                "symbol": "BTC-USDT-SWAP",
                "net_quantity": 1.0,
                "mark_price": 102.5,
                "unrealized_pnl": 3.0,
            }
        ],
    )
    supervisor = RecoverySupervisor(rest_client=rest_client)

    result = await supervisor.run_startup_recovery(
        "BTC-USDT-SWAP",
        checkpoint,
        timestamp="2026-04-19T00:00:00.000Z",
    )

    assert result["run_mode"] == "halted"
    assert result["needs_reconcile"] is True
    assert result["reason"] == "exchange_state_mismatch"
    assert rest_client.account_summary_calls == []


@pytest.mark.asyncio
async def test_recovery_supervisor_blocks_when_checkpoint_and_exchange_diverge() -> None:
    checkpoint = _build_checkpoint()

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
    assert rest_client.account_summary_calls == []

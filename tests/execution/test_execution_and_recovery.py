from datetime import UTC, datetime

import pytest

from xuanshu.checkpoints.service import CheckpointService
from xuanshu.contracts.checkpoint import CheckpointBudgetState, ExecutionCheckpoint
from xuanshu.core.enums import RunMode
from xuanshu.execution.engine import build_client_order_id
from xuanshu.infra.okx.rest import OkxRestClient


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
    healthy_checkpoint = ExecutionCheckpoint(
        checkpoint_id="cp-002",
        created_at=datetime.now(UTC),
        active_snapshot_version="snap-002",
        current_mode=RunMode.NORMAL,
        positions_snapshot=[],
        open_orders_snapshot=[],
        budget_state=CheckpointBudgetState(
            max_daily_loss=100.0,
            remaining_daily_loss=25.0,
            remaining_notional=50.0,
            remaining_order_count=10,
        ),
        last_public_stream_marker=None,
        last_private_stream_marker=None,
        needs_reconcile=False,
    )

    assert build_client_order_id("BTC-USDT-SWAP", "breakout", 7) == "BTCUSDTSWAPbreakout000007"
    assert CheckpointService().can_open_new_risk(checkpoint) is False
    assert CheckpointService().can_open_new_risk(healthy_checkpoint) is True


def test_checkpoint_blocks_new_risk_when_budget_is_exhausted() -> None:
    exhausted_checkpoint = ExecutionCheckpoint(
        checkpoint_id="cp-003",
        created_at=datetime.now(UTC),
        active_snapshot_version="snap-003",
        current_mode=RunMode.NORMAL,
        positions_snapshot=[],
        open_orders_snapshot=[],
        budget_state=CheckpointBudgetState(
            max_daily_loss=100.0,
            remaining_daily_loss=0.0,
            remaining_notional=0.0,
            remaining_order_count=0,
        ),
        last_public_stream_marker="pub-3",
        last_private_stream_marker="pri-3",
        needs_reconcile=False,
    )

    assert CheckpointService().can_open_new_risk(exhausted_checkpoint) is False


@pytest.mark.parametrize(
    ("symbol", "strategy_id", "sequence"),
    [
        ("btc/usdt-swap", "breakout", 7),
        ("BTC-USDT-SWAP", "breakout v2", 7),
        ("BTC-USDT-SWAP", "breakout", -1),
        ("BTC-USDT-SWAP", "breakout", 1_000_000),
    ],
)
def test_build_client_order_id_rejects_unsafe_or_ambiguous_inputs(
    symbol: str,
    strategy_id: str,
    sequence: int,
) -> None:
    with pytest.raises(ValueError):
        build_client_order_id(symbol, strategy_id, sequence)


class _DummyAsyncClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_okx_rest_client_supports_async_context_manager_and_closes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xuanshu.infra.okx.rest.httpx.AsyncClient", _DummyAsyncClient)

    async with OkxRestClient(base_url="https://example.com", api_key="api-key") as client:
        assert isinstance(client.client, _DummyAsyncClient)

    assert client.client.closed == 1

    await client.aclose()

    assert client.client.closed == 1

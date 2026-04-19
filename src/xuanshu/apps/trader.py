from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from xuanshu.checkpoints.service import CheckpointService
from xuanshu.config.settings import TraderRuntimeSettings
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.contracts.checkpoint import CheckpointBudgetState, ExecutionCheckpoint
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.execution.coordinator import ExecutionCoordinator
from xuanshu.execution.engine import build_client_order_id
from xuanshu.infra.okx.private_ws import OkxPrivateStream
from xuanshu.infra.okx.public_ws import OkxPublicStream
from xuanshu.infra.okx.rest import OkxRestClient
from xuanshu.infra.storage.redis_store import (
    RedisRuntimeStateStore,
    RedisSnapshotStore,
    RuntimeStateStore,
    SnapshotStore,
)
from xuanshu.risk.kernel import RiskKernel
from xuanshu.state.engine import StateEngine
from xuanshu.trader.dispatcher import dispatch_event
from xuanshu.trader.recovery import RecoverySupervisor

_OKX_REST_BASE_URL = "https://www.okx.com"
_OKX_PUBLIC_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
_OKX_PRIVATE_WS_URL = "wss://ws.okx.com:8443/ws/v5/private"
_RUN_MODE_PRIORITY = {
    RunMode.NORMAL: 0,
    RunMode.DEGRADED: 1,
    RunMode.REDUCE_ONLY: 2,
    RunMode.HALTED: 3,
}


@dataclass(frozen=True, slots=True)
class TraderComponents:
    state_engine: StateEngine
    risk_kernel: RiskKernel
    checkpoint_service: CheckpointService
    okx_rest_client: OkxRestClient
    okx_public_stream: OkxPublicStream
    okx_private_stream: OkxPrivateStream
    client_order_id_builder: Callable[[str, str, int], str]

    async def aclose(self) -> None:
        await self.okx_rest_client.aclose()


@dataclass(slots=True)
class TraderRuntime:
    settings: TraderRuntimeSettings
    components: TraderComponents
    snapshot_store: SnapshotStore
    runtime_store: RuntimeStateStore
    execution_coordinator: ExecutionCoordinator
    recovery_supervisor: RecoverySupervisor
    starting_nav: float
    startup_snapshot: StrategyConfigSnapshot
    startup_checkpoint: ExecutionCheckpoint
    current_mode: RunMode = RunMode.NORMAL
    opening_allowed: bool = True


def _build_startup_snapshot() -> StrategyConfigSnapshot:
    generated_at = datetime.now(UTC)
    return StrategyConfigSnapshot(
        version_id="bootstrap",
        generated_at=generated_at,
        effective_from=generated_at,
        expires_at=generated_at + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.5,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="bootstrap",
        ttl_sec=300,
    )


def _build_startup_checkpoint(startup_snapshot: StrategyConfigSnapshot) -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        checkpoint_id="startup",
        created_at=datetime.now(UTC),
        active_snapshot_version=startup_snapshot.version_id,
        current_mode=RunMode.NORMAL,
        positions_snapshot=[],
        open_orders_snapshot=[],
        budget_state=CheckpointBudgetState(
            max_daily_loss=100.0,
            remaining_daily_loss=100.0,
            remaining_notional=100.0,
            remaining_order_count=10,
        ),
        last_public_stream_marker=None,
        last_private_stream_marker=None,
        needs_reconcile=False,
    )


def build_trader_components(settings: TraderRuntimeSettings) -> TraderComponents:
    return TraderComponents(
        state_engine=StateEngine(),
        risk_kernel=RiskKernel(nav=settings.trader_starting_nav),
        checkpoint_service=CheckpointService(),
        okx_rest_client=OkxRestClient(
            base_url=_OKX_REST_BASE_URL,
            api_key=settings.okx_api_key.get_secret_value(),
            api_secret=settings.okx_api_secret.get_secret_value(),
            passphrase=settings.okx_api_passphrase.get_secret_value(),
        ),
        okx_public_stream=OkxPublicStream(url=_OKX_PUBLIC_WS_URL),
        okx_private_stream=OkxPrivateStream(url=_OKX_PRIVATE_WS_URL),
        client_order_id_builder=build_client_order_id,
    )


def build_snapshot_store(settings: TraderRuntimeSettings) -> SnapshotStore:
    return RedisSnapshotStore(redis_url=str(settings.redis_url))


def build_runtime_state_store(settings: TraderRuntimeSettings) -> RuntimeStateStore:
    return RedisRuntimeStateStore(redis_url=str(settings.redis_url))


def _more_restrictive_mode(left: RunMode, right: RunMode) -> RunMode:
    if _RUN_MODE_PRIORITY[left] >= _RUN_MODE_PRIORITY[right]:
        return left
    return right


def build_trader_runtime() -> TraderRuntime:
    settings = TraderRuntimeSettings()
    components = build_trader_components(settings)
    snapshot_store = build_snapshot_store(settings)
    startup_snapshot = _build_startup_snapshot()
    latest_snapshot = snapshot_store.get_latest_snapshot()
    if latest_snapshot is not None:
        startup_snapshot = latest_snapshot
    return TraderRuntime(
        settings=settings,
        components=components,
        snapshot_store=snapshot_store,
        runtime_store=build_runtime_state_store(settings),
        execution_coordinator=ExecutionCoordinator(rest_client=components.okx_rest_client),
        recovery_supervisor=RecoverySupervisor(rest_client=components.okx_rest_client),
        starting_nav=settings.trader_starting_nav,
        startup_snapshot=startup_snapshot,
        startup_checkpoint=_build_startup_checkpoint(startup_snapshot),
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _dispatch_runtime_event(runtime: TraderRuntime, event: object) -> None:
    dispatch_event(runtime.components.state_engine, event)
    symbol = getattr(event, "symbol", None)
    if symbol:
        runtime.runtime_store.set_symbol_runtime_summary(
            symbol,
            runtime.components.state_engine.build_symbol_runtime_summary(symbol),
        )
    runtime.runtime_store.set_run_mode(runtime.components.state_engine.current_run_mode)
    runtime.runtime_store.set_fault_flags(runtime.components.state_engine.fault_flags)


async def _run_trader(runtime: TraderRuntime) -> None:
    latest_snapshot = runtime.snapshot_store.get_latest_snapshot()
    if latest_snapshot is not None:
        runtime.startup_snapshot = latest_snapshot
    runtime.startup_checkpoint.active_snapshot_version = runtime.startup_snapshot.version_id
    runtime.opening_allowed = runtime.components.checkpoint_service.can_open_new_risk(runtime.startup_checkpoint)
    runtime.current_mode = runtime.startup_snapshot.market_mode
    if not runtime.opening_allowed:
        runtime.current_mode = _more_restrictive_mode(runtime.current_mode, RunMode.REDUCE_ONLY)
    runtime.startup_snapshot = runtime.startup_snapshot.model_copy(update={"market_mode": runtime.current_mode})
    runtime.startup_checkpoint.current_mode = runtime.current_mode
    runtime.runtime_store.set_run_mode(runtime.current_mode)
    await _wait_forever()


def main() -> int:
    async def _main() -> None:
        runtime = build_trader_runtime()
        try:
            await _run_trader(runtime)
        finally:
            await runtime.components.aclose()

    asyncio.run(_main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

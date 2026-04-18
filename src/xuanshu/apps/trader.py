from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from collections.abc import Callable
from dataclasses import dataclass

from xuanshu.checkpoints.service import CheckpointService
from xuanshu.config.settings import TraderRuntimeSettings
from xuanshu.execution.engine import build_client_order_id
from xuanshu.contracts.checkpoint import CheckpointBudgetState, ExecutionCheckpoint
from xuanshu.core.enums import RunMode
from xuanshu.infra.okx.private_ws import OkxPrivateStream
from xuanshu.infra.okx.public_ws import OkxPublicStream
from xuanshu.infra.okx.rest import OkxRestClient
from xuanshu.risk.kernel import RiskKernel
from xuanshu.state.engine import StateEngine

_OKX_REST_BASE_URL = "https://www.okx.com"
_OKX_PUBLIC_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
_OKX_PRIVATE_WS_URL = "wss://ws.okx.com:8443/ws/v5/private"


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
    starting_nav: float
    startup_checkpoint: ExecutionCheckpoint


def _build_startup_checkpoint() -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        checkpoint_id="startup",
        created_at=datetime.now(UTC),
        active_snapshot_version="bootstrap",
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


def build_trader_runtime() -> TraderRuntime:
    settings = TraderRuntimeSettings()
    return TraderRuntime(
        settings=settings,
        components=build_trader_components(settings),
        starting_nav=settings.trader_starting_nav,
        startup_checkpoint=_build_startup_checkpoint(),
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_trader(runtime: TraderRuntime) -> None:
    runtime.components.checkpoint_service.can_open_new_risk(runtime.startup_checkpoint)
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

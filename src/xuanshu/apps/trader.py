from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Callable

from xuanshu.checkpoints.service import CheckpointService
from xuanshu.config.settings import Settings
from xuanshu.execution.engine import build_client_order_id
from xuanshu.risk.kernel import RiskKernel
from xuanshu.state.engine import StateEngine


@dataclass(frozen=True, slots=True)
class TraderComponents:
    settings: Settings
    state_engine: StateEngine
    risk_kernel: RiskKernel
    checkpoint_service: CheckpointService
    client_order_id_builder: Callable[[str, str, int], str]


def build_trader_components() -> TraderComponents:
    settings = Settings()
    settings.require_trader_runtime()
    return TraderComponents(
        settings=settings,
        state_engine=StateEngine(),
        risk_kernel=RiskKernel(nav=100_000.0),
        checkpoint_service=CheckpointService(),
        client_order_id_builder=build_client_order_id,
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_trader(components: TraderComponents) -> None:
    _ = components
    await _wait_forever()


def main() -> int:
    asyncio.run(_run_trader(build_trader_components()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

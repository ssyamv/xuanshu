from __future__ import annotations

import asyncio

from xuanshu.checkpoints.service import CheckpointService
from xuanshu.execution.engine import build_client_order_id
from xuanshu.risk.kernel import RiskKernel
from xuanshu.state.engine import StateEngine


def build_trader_components() -> dict[str, object]:
    return {
        "state_engine": StateEngine(),
        "risk_kernel": RiskKernel(nav=100_000.0),
        "checkpoint_service": CheckpointService(),
        "client_order_id_builder": build_client_order_id,
    }


async def _wait_forever() -> None:
    await asyncio.Event().wait()


def main() -> int:
    build_trader_components()
    asyncio.run(_wait_forever())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Callable

from xuanshu.checkpoints.service import CheckpointService
from xuanshu.config.settings import TraderRuntimeSettings
from xuanshu.execution.engine import build_client_order_id
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
    settings: TraderRuntimeSettings
    state_engine: StateEngine
    risk_kernel: RiskKernel
    checkpoint_service: CheckpointService
    okx_rest_client: OkxRestClient
    okx_public_stream: OkxPublicStream
    okx_private_stream: OkxPrivateStream
    client_order_id_builder: Callable[[str, str, int], str]

    async def aclose(self) -> None:
        await self.okx_rest_client.aclose()


def build_trader_components() -> TraderComponents:
    settings = TraderRuntimeSettings()
    return TraderComponents(
        settings=settings,
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


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_trader(components: TraderComponents) -> None:
    _ = components
    await _wait_forever()


def main() -> int:
    async def _main() -> None:
        components = build_trader_components()
        try:
            await _run_trader(components)
        finally:
            await components.aclose()

    asyncio.run(_main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

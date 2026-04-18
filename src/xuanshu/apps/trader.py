from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

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
    state_engine: StateEngine
    risk_kernel: RiskKernel
    checkpoint_service: CheckpointService
    okx_rest_client: OkxRestClient
    okx_public_stream: OkxPublicStream
    okx_private_stream: OkxPrivateStream
    client_order_id_builder: Callable[[str, str, int], str]

    async def aclose(self) -> None:
        await self.okx_rest_client.aclose()


@dataclass(frozen=True, slots=True)
class TraderRuntime:
    settings: TraderRuntimeSettings
    components: TraderComponents
    starting_nav: float


def build_trader_components(starting_nav: float) -> TraderComponents:
    settings = TraderRuntimeSettings()
    return TraderComponents(
        state_engine=StateEngine(),
        risk_kernel=RiskKernel(nav=starting_nav),
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
        components=build_trader_components(starting_nav=settings.trader_starting_nav),
        starting_nav=settings.trader_starting_nav,
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_trader(runtime: TraderRuntime) -> None:
    _ = runtime.components
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

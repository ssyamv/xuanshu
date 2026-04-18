from __future__ import annotations

import asyncio
from dataclasses import dataclass

from xuanshu.config.settings import GovernorRuntimeSettings
from xuanshu.governor.service import GovernorService
from xuanshu.infra.ai.governor_client import ConfiguredGovernorAgentRunner, GovernorClient


@dataclass(frozen=True, slots=True)
class GovernorRuntime:
    settings: GovernorRuntimeSettings
    service: GovernorService


def build_governor_service(settings: GovernorRuntimeSettings) -> GovernorService:
    client = GovernorClient(
        agent_runner=ConfiguredGovernorAgentRunner(
            api_key=settings.openai_api_key,
            timeout_sec=settings.ai_timeout_sec,
        )
    )
    return GovernorService(client=client)


def build_governor_runtime() -> GovernorRuntime:
    settings = GovernorRuntimeSettings()
    return GovernorRuntime(settings=settings, service=build_governor_service(settings))


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_governor(runtime: GovernorRuntime) -> None:
    _ = runtime.service
    await _wait_forever()


def main() -> int:
    asyncio.run(_run_governor(build_governor_runtime()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

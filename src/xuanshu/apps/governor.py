from __future__ import annotations

import asyncio
from dataclasses import dataclass

from xuanshu.config.settings import Settings
from xuanshu.governor.service import GovernorService


@dataclass(frozen=True, slots=True)
class GovernorRuntime:
    settings: Settings
    service: GovernorService


def build_governor_service() -> GovernorService:
    return GovernorService()


def build_governor_runtime() -> GovernorRuntime:
    return GovernorRuntime(settings=Settings(), service=build_governor_service())


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

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from xuanshu.config.settings import Settings
from xuanshu.core.enums import RunMode


@dataclass(frozen=True, slots=True)
class NotifierRuntime:
    mode: RunMode
    settings: Settings


def build_notifier_runtime(mode: RunMode | str = RunMode.NORMAL) -> NotifierRuntime:
    settings = Settings()
    settings.require_notifier_runtime()
    return NotifierRuntime(
        mode=mode if isinstance(mode, RunMode) else RunMode(mode),
        settings=settings,
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_notifier(runtime: NotifierRuntime) -> None:
    _ = runtime.mode
    await _wait_forever()


def main() -> int:
    asyncio.run(_run_notifier(build_notifier_runtime()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

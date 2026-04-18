from __future__ import annotations

import asyncio

from xuanshu.core.enums import RunMode
from xuanshu.notifier.service import format_mode_change


def build_notifier_preview(mode: RunMode | str) -> str:
    return format_mode_change(mode if isinstance(mode, RunMode) else RunMode(mode))


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_notifier(preview: str) -> None:
    print(preview)
    await _wait_forever()


def main() -> int:
    asyncio.run(_run_notifier(build_notifier_preview(RunMode.NORMAL)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

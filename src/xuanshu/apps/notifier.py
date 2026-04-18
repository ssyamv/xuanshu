from __future__ import annotations

import asyncio

from xuanshu.core.enums import RunMode
from xuanshu.notifier.service import format_mode_change


def build_notifier_preview(mode: RunMode | str) -> str:
    return format_mode_change(mode if isinstance(mode, RunMode) else RunMode(mode))


async def _wait_forever() -> None:
    await asyncio.Event().wait()


def main() -> int:
    build_notifier_preview(RunMode.NORMAL)
    asyncio.run(_wait_forever())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

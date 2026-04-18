from __future__ import annotations

import asyncio

from xuanshu.governor.service import GovernorService


def build_governor_service() -> GovernorService:
    return GovernorService()


async def _wait_forever() -> None:
    await asyncio.Event().wait()


def main() -> int:
    build_governor_service()
    asyncio.run(_wait_forever())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

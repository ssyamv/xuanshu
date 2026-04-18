from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from xuanshu.config.settings import NotifierRuntimeSettings
from xuanshu.core.enums import RunMode


class NotifierAdapter(Protocol):
    async def send_text(self, text: str) -> None:
        ...


@dataclass(frozen=True, slots=True)
class NotifierRuntime:
    mode: RunMode
    settings: NotifierRuntimeSettings
    adapter: NotifierAdapter


def build_notifier_adapter(settings: NotifierRuntimeSettings) -> NotifierAdapter:
    from xuanshu.infra.notifier.telegram import TelegramNotifier

    return TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )


def build_notifier_runtime(mode: RunMode | str = RunMode.NORMAL) -> NotifierRuntime:
    settings = NotifierRuntimeSettings()
    return NotifierRuntime(
        mode=mode if isinstance(mode, RunMode) else RunMode(mode),
        settings=settings,
        adapter=build_notifier_adapter(settings),
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_notifier(runtime: NotifierRuntime) -> None:
    await runtime.adapter.send_text("Notifier runtime started")
    await _wait_forever()


def main() -> int:
    asyncio.run(_run_notifier(build_notifier_runtime()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

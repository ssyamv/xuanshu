from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from xuanshu.config.settings import NotifierRuntimeSettings
from xuanshu.core.enums import RunMode
from xuanshu.infra.notifier.telegram import TelegramInboundMessage, TextMessagePayload
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.infra.storage.redis_store import RedisRuntimeStateStore, RedisSnapshotStore
from xuanshu.notifier.service import NotifierService


class NotifierAdapter(Protocol):
    async def send_text(self, payload: TextMessagePayload) -> None:
        ...

    async def fetch_updates(
        self,
        offset: int | None = None,
        limit: int = 20,
        timeout_sec: int = 30,
    ) -> list[TelegramInboundMessage]:
        ...


@dataclass(slots=True)
class NotifierRuntime:
    mode: RunMode
    settings: NotifierRuntimeSettings
    adapter: NotifierAdapter
    service: NotifierService
    snapshot_store: RedisSnapshotStore
    runtime_store: RedisRuntimeStateStore
    history_store: PostgresRuntimeStore
    next_update_offset: int | None = None


def build_notifier_adapter(settings: NotifierRuntimeSettings) -> NotifierAdapter:
    from xuanshu.infra.notifier.telegram import TelegramNotifier

    return TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )


def build_snapshot_store(settings: NotifierRuntimeSettings) -> RedisSnapshotStore:
    return RedisSnapshotStore(redis_url=str(settings.redis_url))


def build_runtime_state_store(settings: NotifierRuntimeSettings) -> RedisRuntimeStateStore:
    return RedisRuntimeStateStore(redis_url=str(settings.redis_url))


def build_history_store(settings: NotifierRuntimeSettings) -> PostgresRuntimeStore:
    return PostgresRuntimeStore(dsn=str(settings.postgres_dsn))


def build_notifier_service(
    settings: NotifierRuntimeSettings,
    *,
    runtime_store: RedisRuntimeStateStore,
    snapshot_store: RedisSnapshotStore,
    history_store: PostgresRuntimeStore,
) -> NotifierService:
    return NotifierService(
        okx_symbols=settings.okx_symbols,
        runtime_store=runtime_store,
        snapshot_store=snapshot_store,
        history_store=history_store,
    )


def build_notifier_runtime(mode: RunMode | str = RunMode.NORMAL) -> NotifierRuntime:
    settings = NotifierRuntimeSettings()
    snapshot_store = build_snapshot_store(settings)
    runtime_store = build_runtime_state_store(settings)
    history_store = build_history_store(settings)
    return NotifierRuntime(
        mode=mode if isinstance(mode, RunMode) else RunMode(mode),
        settings=settings,
        adapter=build_notifier_adapter(settings),
        service=build_notifier_service(
            settings,
            runtime_store=runtime_store,
            snapshot_store=snapshot_store,
            history_store=history_store,
        ),
        snapshot_store=snapshot_store,
        runtime_store=runtime_store,
        history_store=history_store,
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _wait_for_next_poll(delay_sec: float = 1.0) -> None:
    await asyncio.sleep(delay_sec)


async def _poll_notifier_once(runtime: NotifierRuntime) -> None:
    updates = await runtime.adapter.fetch_updates(offset=runtime.next_update_offset)
    if not updates:
        return
    for update in updates:
        runtime.next_update_offset = update.update_id + 1
        if update.chat_id != runtime.settings.telegram_chat_id:
            continue
        payload = await runtime.service.handle_command(update.text)
        try:
            await runtime.service.deliver_text(
                adapter=runtime.adapter,
                text=payload.text,
                severity="INFO",
                category="command_response",
                dedupe_key=f"command:{update.update_id}",
            )
        except Exception:
            continue


async def _run_command_loop(runtime: NotifierRuntime) -> None:
    while True:
        await _poll_notifier_once(runtime)
        await runtime.service.flush_proactive_notifications(adapter=runtime.adapter)
        await runtime.service.flush_pending_notifications(adapter=runtime.adapter)
        await _wait_for_next_poll()


async def _run_notifier(runtime: NotifierRuntime) -> None:
    try:
        await runtime.service.deliver_text(
            adapter=runtime.adapter,
            text="Notifier runtime started",
            severity="INFO",
            category="runtime_started",
            dedupe_key="runtime_started",
        )
    except Exception:
        pass

    if hasattr(runtime.adapter, "fetch_updates"):
        await _run_command_loop(runtime)
        return
    await _wait_forever()


def main() -> int:
    asyncio.run(_run_notifier(build_notifier_runtime()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import asyncio

import pytest
from pydantic import ValidationError

import xuanshu.apps.notifier as notifier_app
from xuanshu.core.enums import RunMode
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.infra.notifier.telegram import TextMessagePayload
from xuanshu.infra.storage.redis_store import RedisRuntimeStateStore, RedisSnapshotStore


def _set_required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")


def _clear_unrelated_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "QDRANT_URL",
        "OPENAI_API_KEY",
        "OKX_API_KEY",
        "OKX_API_SECRET",
        "OKX_API_PASSPHRASE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_notifier_entrypoint_loads_settings_and_threads_it_into_runtime(monkeypatch, capsys) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)

    seen_runtime = None

    async def _noop_wait_forever() -> None:
        return None

    async def fake_run_notifier(runtime: notifier_app.NotifierRuntime) -> None:
        nonlocal seen_runtime
        seen_runtime = runtime
        await _noop_wait_forever()

    monkeypatch.setattr(notifier_app, "_run_notifier", fake_run_notifier)

    assert notifier_app.main() == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert seen_runtime is not None
    assert seen_runtime.mode == RunMode.NORMAL
    assert seen_runtime.settings.telegram_chat_id == "123456"
    assert hasattr(seen_runtime.adapter, "send_text")
    assert not hasattr(seen_runtime, "notifier")


def test_notifier_entrypoint_fails_fast_without_required_settings(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    _clear_unrelated_settings_env(monkeypatch)

    async def unexpected_run_notifier(_: notifier_app.NotifierRuntime) -> None:
        raise AssertionError("notifier runtime should not start when settings are invalid")

    monkeypatch.setattr(notifier_app, "_run_notifier", unexpected_run_notifier)

    with pytest.raises(ValidationError):
        notifier_app.main()


def test_notifier_entrypoint_fails_fast_without_telegram_wiring(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    _clear_unrelated_settings_env(monkeypatch)

    async def unexpected_run_notifier(_: notifier_app.NotifierRuntime) -> None:
        raise AssertionError("notifier runtime should not start when Telegram wiring is invalid")

    monkeypatch.setattr(notifier_app, "_run_notifier", unexpected_run_notifier)

    with pytest.raises(ValidationError):
        notifier_app.main()


def test_notifier_runtime_sends_payload_via_adapter(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)

    delivered = []

    class _Adapter:
        async def send_text(self, payload: TextMessagePayload):
            delivered.append(payload.text)

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(notifier_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(notifier_app, "build_notifier_adapter", lambda settings: _Adapter())

    runtime = notifier_app.build_notifier_runtime()
    asyncio.run(notifier_app._run_notifier(runtime))

    assert delivered == ["Notifier runtime started"]


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def set(self, key: str, value: str) -> bool:
        self.values[key] = value.encode("utf-8")
        return True

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)


def test_notifier_runtime_wires_runtime_state_and_history_stores(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql+psycopg://xuanshu:xuanshu@localhost:5432/xuanshu")
    _clear_unrelated_settings_env(monkeypatch)

    runtime = notifier_app.build_notifier_runtime()

    assert isinstance(runtime.snapshot_store, RedisSnapshotStore)
    assert isinstance(runtime.runtime_store, RedisRuntimeStateStore)
    assert isinstance(runtime.history_store, PostgresRuntimeStore)


def test_notifier_runtime_processes_one_command_poll(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _Adapter:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_text(self, payload: TextMessagePayload):
            self.sent.append(payload.text)

        async def fetch_updates(self, offset: int | None = None, limit: int = 20, timeout_sec: int = 30):
            return [
                notifier_app.TelegramInboundMessage(
                    update_id=offset or 1,
                    chat_id="123456",
                    text="/mode",
                )
            ]

    adapter = _Adapter()
    monkeypatch.setattr(notifier_app, "build_notifier_adapter", lambda settings: adapter)
    monkeypatch.setattr(
        notifier_app,
        "build_snapshot_store",
        lambda settings: RedisSnapshotStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(
        notifier_app,
        "build_runtime_state_store",
        lambda settings: RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(
        notifier_app,
        "build_history_store",
        lambda settings: PostgresRuntimeStore(
            dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu"
        ),
    )

    runtime = notifier_app.build_notifier_runtime()
    runtime.runtime_store.set_run_mode(RunMode.REDUCE_ONLY)

    asyncio.run(notifier_app._poll_notifier_once(runtime))

    assert adapter.sent == ["Mode: reduce_only"]
    assert runtime.next_update_offset == 2


def test_notifier_runtime_processes_manual_takeover_command(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _Adapter:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_text(self, payload: TextMessagePayload):
            self.sent.append(payload.text)

        async def fetch_updates(self, offset: int | None = None, limit: int = 20, timeout_sec: int = 30):
            return [
                notifier_app.TelegramInboundMessage(
                    update_id=offset or 1,
                    chat_id="123456",
                    text="/takeover halted operator requested stop",
                )
            ]

    adapter = _Adapter()
    store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    monkeypatch.setattr(notifier_app, "build_notifier_adapter", lambda settings: adapter)
    monkeypatch.setattr(
        notifier_app,
        "build_snapshot_store",
        lambda settings: RedisSnapshotStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(
        notifier_app,
        "build_runtime_state_store",
        lambda settings: RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(notifier_app, "build_history_store", lambda settings: store)

    runtime = notifier_app.build_notifier_runtime()

    asyncio.run(notifier_app._poll_notifier_once(runtime))

    assert adapter.sent == ["Manual takeover requested: halted (reason=operator requested stop)"]
    assert runtime.runtime_store.get_run_mode() == RunMode.HALTED
    assert store.list_recent_rows("risk_events", limit=1) == [
        {
            "event_type": "manual_takeover_requested",
            "symbol": "system",
            "detail": "requested halted: operator requested stop",
        }
    ]


def test_notifier_command_loop_flushes_retry_queue(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _Adapter:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_text(self, payload: TextMessagePayload):
            self.sent.append(payload.text)

        async def fetch_updates(self, offset: int | None = None, limit: int = 20, timeout_sec: int = 30):
            return []

    adapter = _Adapter()
    store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    store.append_notification_event(
        {
            "category": "mode_change",
            "dedupe_key": "mode:halted",
            "severity": "CRITICAL",
            "status": "failed",
            "attempt_count": 3,
            "needs_retry": True,
            "text": "entered halted mode",
        }
    )

    monkeypatch.setattr(notifier_app, "build_notifier_adapter", lambda settings: adapter)
    monkeypatch.setattr(
        notifier_app,
        "build_snapshot_store",
        lambda settings: RedisSnapshotStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(
        notifier_app,
        "build_runtime_state_store",
        lambda settings: RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(notifier_app, "build_history_store", lambda settings: store)

    runtime = notifier_app.build_notifier_runtime()

    calls = 0

    async def _stop_after_one_poll(delay_sec: float = 1.0) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("stop loop")

    monkeypatch.setattr(notifier_app, "_wait_for_next_poll", _stop_after_one_poll)

    with pytest.raises(RuntimeError, match="stop loop"):
        asyncio.run(notifier_app._run_command_loop(runtime))

    assert adapter.sent == ["entered halted mode"]


def test_notifier_command_loop_flushes_proactive_notifications(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _Adapter:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_text(self, payload: TextMessagePayload):
            self.sent.append(payload.text)

        async def fetch_updates(self, offset: int | None = None, limit: int = 20, timeout_sec: int = 30):
            return []

    adapter = _Adapter()
    store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    store.save_checkpoint(
        {
            "checkpoint_id": "recovery-001",
            "current_mode": "reduce_only",
            "needs_reconcile": False,
        }
    )

    monkeypatch.setattr(notifier_app, "build_notifier_adapter", lambda settings: adapter)
    monkeypatch.setattr(
        notifier_app,
        "build_snapshot_store",
        lambda settings: RedisSnapshotStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(
        notifier_app,
        "build_runtime_state_store",
        lambda settings: RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(notifier_app, "build_history_store", lambda settings: store)

    runtime = notifier_app.build_notifier_runtime()

    async def _stop_after_one_poll(delay_sec: float = 1.0) -> None:
        raise RuntimeError("stop loop")

    monkeypatch.setattr(notifier_app, "_wait_for_next_poll", _stop_after_one_poll)

    with pytest.raises(RuntimeError, match="stop loop"):
        asyncio.run(notifier_app._run_command_loop(runtime))

    assert adapter.sent == ["Mode changed to reduce-only"]

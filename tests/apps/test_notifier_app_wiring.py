import asyncio

import pytest
from pydantic import ValidationError

import xuanshu.apps.notifier as notifier_app
from xuanshu.core.enums import RunMode


def _set_required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")


def _clear_unrelated_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REDIS_URL",
        "POSTGRES_DSN",
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
        async def send_text(self, text):
            delivered.append(text)

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(notifier_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(notifier_app, "build_notifier_adapter", lambda settings: _Adapter())

    runtime = notifier_app.build_notifier_runtime()
    asyncio.run(notifier_app._run_notifier(runtime))

    assert delivered == ["Notifier runtime started"]

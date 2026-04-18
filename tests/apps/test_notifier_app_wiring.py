import pytest
from pydantic import ValidationError

import xuanshu.apps.notifier as notifier_app
from xuanshu.core.enums import RunMode


def _set_required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("XUANSHU_OKX_SYMBOLS", "BTC-USDT-SWAP, ETH-USDT-SWAP")


def test_notifier_entrypoint_loads_settings_and_threads_it_into_runtime(monkeypatch, capsys) -> None:
    _set_required_settings_env(monkeypatch)

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
    assert seen_runtime.settings.okx_symbols == ("BTC-USDT-SWAP", "ETH-USDT-SWAP")


def test_notifier_entrypoint_fails_fast_without_required_settings(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("QDRANT_URL", raising=False)

    async def unexpected_run_notifier(_: notifier_app.NotifierRuntime) -> None:
        raise AssertionError("notifier runtime should not start when settings are invalid")

    monkeypatch.setattr(notifier_app, "_run_notifier", unexpected_run_notifier)

    with pytest.raises(ValidationError):
        notifier_app.main()

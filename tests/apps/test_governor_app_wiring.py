import pytest
from pydantic import ValidationError

import xuanshu.apps.governor as governor_app


def _set_required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("XUANSHU_OKX_SYMBOLS", "BTC-USDT-SWAP, ETH-USDT-SWAP")


def test_governor_entrypoint_loads_settings_and_threads_it_into_runtime(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)

    seen_runtime = None

    async def _noop_wait_forever() -> None:
        return None

    async def fake_run_governor(runtime: governor_app.GovernorRuntime) -> None:
        nonlocal seen_runtime
        seen_runtime = runtime
        await _noop_wait_forever()

    monkeypatch.setattr(governor_app, "_run_governor", fake_run_governor)

    assert governor_app.main() == 0

    assert seen_runtime is not None
    assert seen_runtime.service.__class__.__name__ == "GovernorService"
    assert seen_runtime.settings.okx_symbols == ("BTC-USDT-SWAP", "ETH-USDT-SWAP")


def test_governor_entrypoint_fails_fast_without_required_settings(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("QDRANT_URL", raising=False)

    async def unexpected_run_governor(_: governor_app.GovernorRuntime) -> None:
        raise AssertionError("governor runtime should not start when settings are invalid")

    monkeypatch.setattr(governor_app, "_run_governor", unexpected_run_governor)

    with pytest.raises(ValidationError):
        governor_app.main()

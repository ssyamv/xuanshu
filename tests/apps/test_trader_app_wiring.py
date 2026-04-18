import pytest
from pydantic import ValidationError

import xuanshu.apps.trader as trader_app


def _set_required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("XUANSHU_OKX_SYMBOLS", "BTC-USDT-SWAP, ETH-USDT-SWAP")
    monkeypatch.setenv("OKX_API_KEY", "api-key")
    monkeypatch.setenv("OKX_API_SECRET", "api-secret")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "api-passphrase")


def test_trader_entrypoint_loads_settings_and_threads_it_into_components(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)

    seen_components = None

    async def _noop_wait_forever() -> None:
        return None

    async def fake_run_trader(components: trader_app.TraderComponents) -> None:
        nonlocal seen_components
        seen_components = components
        await _noop_wait_forever()

    monkeypatch.setattr(trader_app, "_run_trader", fake_run_trader)

    assert trader_app.main() == 0

    assert seen_components is not None
    assert seen_components.settings.okx_symbols == ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    assert seen_components.state_engine.__class__.__name__ == "StateEngine"
    assert seen_components.risk_kernel.nav == 100_000.0
    assert seen_components.checkpoint_service.__class__.__name__ == "CheckpointService"
    assert seen_components.client_order_id_builder("BTC-USDT-SWAP", "breakout", 1) == "BTC-USDT-SWAP-breakout-000001"
    assert seen_components.settings.okx_api_key.get_secret_value() == "api-key"


def test_trader_entrypoint_fails_fast_without_required_settings(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("QDRANT_URL", raising=False)

    async def unexpected_run_trader(_: trader_app.TraderComponents) -> None:
        raise AssertionError("trader runtime should not start when settings are invalid")

    monkeypatch.setattr(trader_app, "_run_trader", unexpected_run_trader)

    with pytest.raises(ValidationError):
        trader_app.main()


def test_trader_entrypoint_fails_fast_without_okx_credentials(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("OKX_API_KEY", "")

    async def unexpected_run_trader(_: trader_app.TraderComponents) -> None:
        raise AssertionError("trader runtime should not start when OKX credentials are invalid")

    monkeypatch.setattr(trader_app, "_run_trader", unexpected_run_trader)

    with pytest.raises(ValidationError):
        trader_app.main()

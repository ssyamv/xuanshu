from pathlib import Path

import pytest
from pydantic import ValidationError

import xuanshu.apps.trader as trader_app
from xuanshu.infra.okx.private_ws import OkxPrivateStream
from xuanshu.infra.okx.public_ws import OkxPublicStream
from xuanshu.infra.okx.rest import OkxRestClient


def _set_required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XUANSHU_OKX_SYMBOLS", "BTC-USDT-SWAP, ETH-USDT-SWAP")
    monkeypatch.setenv("XUANSHU_TRADER_STARTING_NAV", "250000")
    monkeypatch.setenv("OKX_API_KEY", "api-key")
    monkeypatch.setenv("OKX_API_SECRET", "api-secret")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "api-passphrase")


def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "XUANSHU_OKX_SYMBOLS",
        "XUANSHU_TRADER_STARTING_NAV",
        "OKX_API_KEY",
        "OKX_API_SECRET",
        "OKX_API_PASSPHRASE",
    ):
        monkeypatch.delenv(key, raising=False)


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
    assert seen_components.risk_kernel.nav == 250_000.0
    assert seen_components.checkpoint_service.__class__.__name__ == "CheckpointService"
    assert isinstance(seen_components.okx_rest_client, OkxRestClient)
    assert isinstance(seen_components.okx_public_stream, OkxPublicStream)
    assert isinstance(seen_components.okx_private_stream, OkxPrivateStream)
    assert seen_components.client_order_id_builder("BTC-USDT-SWAP", "breakout", 1) == "BTC-USDT-SWAP-breakout-000001"
    assert seen_components.settings.okx_api_key.get_secret_value() == "api-key"
    assert seen_components.okx_rest_client.api_key == "api-key"
    assert seen_components.okx_public_stream.url.endswith("/public")
    assert seen_components.okx_private_stream.url.endswith("/private")


def test_trader_entrypoint_loads_runtime_from_dotenv_startup_path(monkeypatch, tmp_path) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text(
        "\n".join(
            [
                "XUANSHU_OKX_SYMBOLS=BTC-USDT-SWAP,ETH-USDT-SWAP",
                "OKX_API_KEY=api-key",
                "OKX_API_SECRET=api-secret",
                "OKX_API_PASSPHRASE=api-passphrase",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    async def fake_run_trader(components: trader_app.TraderComponents) -> None:
        assert components.risk_kernel.nav == 250_000.0
        await components.okx_rest_client.aclose()

    monkeypatch.setattr(trader_app, "_run_trader", fake_run_trader)

    assert trader_app.main() == 0


def test_trader_runtime_contract_lists_starting_nav() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "XUANSHU_TRADER_STARTING_NAV=" in env_example
    assert "XUANSHU_TRADER_STARTING_NAV:" in compose


def test_trader_entrypoint_fails_fast_without_required_settings(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_TRADER_STARTING_NAV", "0")

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

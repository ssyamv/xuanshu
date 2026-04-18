import pytest
from pydantic import ValidationError

import xuanshu.apps.governor as governor_app
from xuanshu.infra.ai.governor_client import ConfiguredGovernorAgentRunner, GovernorClient


def _set_required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")


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
    assert seen_runtime.settings.openai_api_key.get_secret_value() == "openai-key"
    assert isinstance(seen_runtime.service.client, GovernorClient)
    assert isinstance(seen_runtime.service.client.agent_runner, ConfiguredGovernorAgentRunner)
    assert seen_runtime.service.client.agent_runner.api_key.get_secret_value() == "openai-key"
    assert seen_runtime.service.client.agent_runner.timeout_sec == 12


def test_governor_entrypoint_fails_fast_without_required_settings(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")

    async def unexpected_run_governor(_: governor_app.GovernorRuntime) -> None:
        raise AssertionError("governor runtime should not start when settings are invalid")

    monkeypatch.setattr(governor_app, "_run_governor", unexpected_run_governor)

    with pytest.raises(ValidationError):
        governor_app.main()


def test_governor_entrypoint_fails_fast_without_openai_api_key(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "")

    async def unexpected_run_governor(_: governor_app.GovernorRuntime) -> None:
        raise AssertionError("governor runtime should not start when OpenAI credentials are invalid")

    monkeypatch.setattr(governor_app, "_run_governor", unexpected_run_governor)

    with pytest.raises(ValidationError):
        governor_app.main()

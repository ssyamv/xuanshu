import asyncio

import pytest
from pydantic import ValidationError

import xuanshu.apps.governor as governor_app
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.infra.ai.governor_client import ConfiguredGovernorAgentRunner, GovernorClient


def _set_required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")


def _clear_unrelated_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REDIS_URL",
        "POSTGRES_DSN",
        "QDRANT_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "OKX_API_KEY",
        "OKX_API_SECRET",
        "OKX_API_PASSPHRASE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_governor_entrypoint_loads_settings_and_threads_it_into_runtime(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)

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
    assert isinstance(seen_runtime.governor_client, GovernorClient)
    assert isinstance(seen_runtime.governor_client.agent_runner, ConfiguredGovernorAgentRunner)
    assert seen_runtime.governor_client.agent_runner.api_key.get_secret_value() == "openai-key"
    assert seen_runtime.governor_client.agent_runner.timeout_sec == 12
    assert seen_runtime.last_snapshot.version_id == "bootstrap"


def test_governor_entrypoint_fails_fast_without_required_settings(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    _clear_unrelated_settings_env(monkeypatch)

    async def unexpected_run_governor(_: governor_app.GovernorRuntime) -> None:
        raise AssertionError("governor runtime should not start when settings are invalid")

    monkeypatch.setattr(governor_app, "_run_governor", unexpected_run_governor)

    with pytest.raises(ValidationError):
        governor_app.main()


def test_governor_runtime_runs_one_cycle_and_publishes_snapshot(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)

    class _Runner:
        async def run(self, state_summary):
            assert state_summary == {"scope": "governor"}
            return {
                "version_id": "snap-new",
                "generated_at": "2026-04-18T00:00:00Z",
                "effective_from": "2026-04-18T00:00:00Z",
                "expires_at": "2026-04-18T00:05:00Z",
                "symbol_whitelist": ["BTC-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
                "risk_multiplier": 0.5,
                "per_symbol_max_position": 0.12,
                "max_leverage": 3,
                "market_mode": RunMode.NORMAL,
                "approval_state": ApprovalState.APPROVED,
                "source_reason": "cycle",
                "ttl_sec": 300,
            }

    monkeypatch.setattr(
        governor_app,
        "build_governor_client",
        lambda settings: GovernorClient(_Runner()),
    )

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)

    runtime = governor_app.build_governor_runtime()
    asyncio.run(governor_app._run_governor(runtime))

    assert runtime.last_snapshot.version_id == "snap-new"
    assert [snapshot.version_id for snapshot in runtime.published_snapshots] == ["snap-new"]


def test_governor_runtime_publishes_snapshot_to_shared_file_store(monkeypatch, tmp_path) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)

    class _Runner:
        async def run(self, state_summary):
            return {
                "version_id": "snap-shared",
                "generated_at": "2026-04-18T00:00:00Z",
                "effective_from": "2026-04-18T00:00:00Z",
                "expires_at": "2026-04-18T00:05:00Z",
                "symbol_whitelist": ["BTC-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
                "risk_multiplier": 0.5,
                "per_symbol_max_position": 0.12,
                "max_leverage": 3,
                "market_mode": RunMode.NORMAL,
                "approval_state": ApprovalState.APPROVED,
                "source_reason": "cycle",
                "ttl_sec": 300,
            }

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setenv("XUANSHU_SHARED_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))

    runtime = governor_app.build_governor_runtime()
    asyncio.run(governor_app._run_governor(runtime))

    stored = runtime.snapshot_store.get_latest_snapshot()
    assert stored is not None
    assert stored.version_id == "snap-shared"

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import xuanshu.apps.governor as governor_app
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.infra.ai.governor_client import ConfiguredGovernorAgentRunner, GovernorClient
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.infra.storage.redis_store import RedisSnapshotStore


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def set(self, key: str, value: str) -> bool:
        self.values[key] = value.encode("utf-8")
        return True

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)


def _set_required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql+psycopg://xuanshu:xuanshu@localhost:5432/xuanshu")


def _clear_unrelated_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REDIS_URL",
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
    assert seen_runtime.history_store.dsn == "postgresql+psycopg://xuanshu:xuanshu@localhost:5432/xuanshu"
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
    captured_state_summary = None

    class _Runner:
        async def run(self, state_summary):
            nonlocal captured_state_summary
            captured_state_summary = state_summary
            now = datetime.now(UTC)
            return {
                "version_id": "snap-new",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert captured_state_summary["scope"] == "governor"
    assert captured_state_summary["current_run_mode"] == "unknown"
    assert captured_state_summary["latest_snapshot_version"] == "bootstrap"
    assert captured_state_summary["active_fault_flags"] == []
    assert captured_state_summary["symbol_summaries"] == []
    assert captured_state_summary["recent_risk_events"] == []
    assert captured_state_summary["recent_governor_runs"] == []
    assert captured_state_summary["trigger_reason"] == "schedule"
    assert captured_state_summary["committee_summary"] == {
        "consensus_decision": "maintain",
        "recommended_mode_floor": "normal",
        "blocking_flags": [],
        "requires_human_review": False,
        "active_experts": ["market_structure", "risk", "event_filter"],
    }
    assert [opinion["expert_type"] for opinion in captured_state_summary["expert_opinions"]] == [
        "market_structure",
        "risk",
        "event_filter",
    ]
    assert runtime.last_snapshot.version_id == "snap-new"
    assert [snapshot.version_id for snapshot in runtime.published_snapshots] == ["snap-new"]


def test_governor_runtime_publishes_snapshot_to_shared_redis_store(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)

    class _Runner:
        async def run(self, state_summary):
            return {
                "version_id": "snap-shared",
                "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "effective_from": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(
        governor_app,
        "build_snapshot_store",
        lambda settings: RedisSnapshotStore(redis_client=_FakeRedis()),
    )

    runtime = governor_app.build_governor_runtime()
    asyncio.run(governor_app._run_governor_cycle(runtime))

    stored = runtime.snapshot_store.get_latest_snapshot()
    assert stored is not None
    assert stored.version_id == "snap-shared"


def test_governor_runtime_records_snapshot_publication_for_notifier(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-audit",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "symbol_whitelist": ["BTC-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
                "risk_multiplier": 0.5,
                "per_symbol_max_position": 0.12,
                "max_leverage": 3,
                "market_mode": RunMode.DEGRADED,
                "approval_state": ApprovalState.APPROVED,
                "source_reason": "cycle",
                "ttl_sec": 300,
            }

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)

    runtime = governor_app.build_governor_runtime()
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert history_store.written_rows["strategy_snapshots"] == [
        {
            "version_id": "snap-audit",
            "market_mode": "degraded",
            "approval_state": "approved",
        }
    ]
    assert history_store.written_rows["governor_runs"] == [
        {
            "version_id": "snap-audit",
            "status": "published",
        }
    ]
    assert [row["expert_type"] for row in history_store.written_rows["expert_opinions"]] == [
        "market_structure",
        "risk",
        "event_filter",
    ]


def test_governor_runtime_enriches_state_summary_with_qdrant_cases(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    captured_state_summary = None
    seen_queries: list[dict[str, object]] = []

    class _Runner:
        async def run(self, state_summary):
            nonlocal captured_state_summary
            captured_state_summary = state_summary
            now = datetime.now(UTC)
            return {
                "version_id": "snap-cases",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    class _CaseStore:
        def search_governance_cases(self, query: dict[str, object], limit: int = 3) -> list[dict[str, object]]:
            seen_queries.append(query)
            return [
                {
                    "case_id": "gov-001",
                    "summary": "manual takeover required after repeated recovery failures",
                    "recommended_mode": "halted",
                }
            ]

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_case_store", lambda settings: _CaseStore())

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_run_mode(RunMode.DEGRADED)
    runtime.history_store.append_risk_event({"event_type": "runtime_mode_changed", "detail": "reduced risk"})
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert seen_queries == [
        {
            "trigger_reason": "risk_event",
            "current_run_mode": "degraded",
            "recommended_mode_floor": "degraded",
            "active_fault_flags": [],
        }
    ]
    assert captured_state_summary["governance_cases"] == [
        {
            "case_id": "gov-001",
            "summary": "manual takeover required after repeated recovery failures",
            "recommended_mode": "halted",
        }
    ]


def test_governor_runtime_publishes_health_summary_and_trigger_reason(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-health",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.history_store.append_risk_event({"event_type": "runtime_mode_changed", "detail": "reduced risk"})
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.runtime_store.get_governor_health_summary() == {
        "status": "published",
        "trigger": "risk_event",
        "snapshot_version": "snap-health",
        "market_mode": "degraded",
        "approval_state": "approved",
        "risk_multiplier": 0.25,
        "consecutive_failures": 0,
        "health_state": "healthy",
    }


def test_governor_loop_runs_multiple_cycles_on_schedule(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_GOVERNOR_INTERVAL_SEC", "5")
    _clear_unrelated_settings_env(monkeypatch)
    seen_state_summaries: list[dict[str, object]] = []

    class _Runner:
        async def run(self, state_summary):
            seen_state_summaries.append(dict(state_summary))
            version = f"snap-{len(seen_state_summaries)}"
            now = datetime.now(UTC)
            return {
                "version_id": version,
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    waits: list[int] = []

    async def _fake_wait_for_next_cycle(delay_sec: int) -> None:
        waits.append(delay_sec)
        if len(waits) >= 2:
            raise RuntimeError("stop loop")

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "_wait_for_next_cycle", _fake_wait_for_next_cycle)

    runtime = governor_app.build_governor_runtime()

    with pytest.raises(RuntimeError, match="stop loop"):
        asyncio.run(governor_app._run_governor_loop(runtime))

    assert [snapshot.version_id for snapshot in runtime.published_snapshots] == ["snap-1", "snap-2"]
    assert waits == [5, 5]


def test_governor_loop_short_circuits_wait_on_event_trigger(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_GOVERNOR_INTERVAL_SEC", "30")
    _clear_unrelated_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            version = f"snap-{state_summary['trigger_reason']}"
            now = datetime.now(UTC)
            return {
                "version_id": version,
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    waits: list[int] = []

    async def _fake_wait_for_next_cycle(delay_sec: int) -> None:
        waits.append(delay_sec)
        if len(waits) == 1:
            runtime.history_store.append_risk_event({"event_type": "runtime_mode_changed", "detail": "reduced risk"})
            return
        raise RuntimeError("stop loop")

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(governor_app, "_wait_for_next_cycle", _fake_wait_for_next_cycle)

    runtime = governor_app.build_governor_runtime()

    with pytest.raises(RuntimeError, match="stop loop"):
        asyncio.run(governor_app._run_governor_loop(runtime))

    assert [snapshot.version_id for snapshot in runtime.published_snapshots] == ["snap-schedule", "snap-risk_event"]
    assert waits == [30, 0]


def test_governor_cycle_freezes_snapshot_and_tracks_consecutive_failures(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            raise RuntimeError("llm timeout")

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    runtime = governor_app.build_governor_runtime()

    asyncio.run(governor_app._run_governor_cycle(runtime))
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.last_snapshot.version_id == "bootstrap"
    assert runtime.consecutive_failures == 2
    assert runtime.runtime_store.get_governor_health_summary() == {
        "status": "frozen",
        "trigger": "schedule",
        "snapshot_version": "bootstrap",
        "market_mode": "normal",
        "approval_state": "approved",
        "risk_multiplier": 0.5,
        "consecutive_failures": 2,
        "health_state": "healthy",
    }


def test_governor_cycle_tightens_health_state_after_three_failures(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            raise RuntimeError("llm timeout")

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    runtime = governor_app.build_governor_runtime()

    asyncio.run(governor_app._run_governor_cycle(runtime))
    asyncio.run(governor_app._run_governor_cycle(runtime))
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.consecutive_failures == 3
    assert runtime.runtime_store.get_governor_health_summary() == {
        "status": "frozen",
        "trigger": "schedule",
        "snapshot_version": "bootstrap",
        "market_mode": "degraded",
        "approval_state": "approved",
        "risk_multiplier": 0.5,
        "consecutive_failures": 3,
        "health_state": "degraded",
    }

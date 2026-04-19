from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from xuanshu.config.settings import GovernorRuntimeSettings
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.governor.service import GovernorService
from xuanshu.infra.ai.governor_client import ConfiguredGovernorAgentRunner, GovernorClient
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.infra.storage.qdrant_store import QdrantCaseStore
from xuanshu.infra.storage.redis_store import (
    RedisRuntimeStateStore,
    RedisSnapshotStore,
    RuntimeStateStore,
    SnapshotStore,
)


@dataclass(slots=True)
class GovernorRuntime:
    settings: GovernorRuntimeSettings
    service: GovernorService
    governor_client: GovernorClient
    case_store: QdrantCaseStore
    snapshot_store: SnapshotStore
    runtime_store: RuntimeStateStore
    history_store: PostgresRuntimeStore
    last_snapshot: StrategyConfigSnapshot
    published_snapshots: list[StrategyConfigSnapshot] = field(default_factory=list)
    consecutive_failures: int = 0


def build_governor_service() -> GovernorService:
    return GovernorService()


def build_governor_client(settings: GovernorRuntimeSettings) -> GovernorClient:
    return GovernorClient(
        agent_runner=ConfiguredGovernorAgentRunner(
            api_key=settings.openai_api_key,
            timeout_sec=settings.ai_timeout_sec,
        )
    )


def build_snapshot_store(settings: GovernorRuntimeSettings) -> SnapshotStore:
    return RedisSnapshotStore(redis_url=str(settings.redis_url))


def build_runtime_state_store(settings: GovernorRuntimeSettings) -> RuntimeStateStore:
    return RedisRuntimeStateStore(redis_url=str(settings.redis_url))


def build_history_store(settings: GovernorRuntimeSettings) -> PostgresRuntimeStore:
    return PostgresRuntimeStore(dsn=str(settings.postgres_dsn))


def build_case_store(settings: GovernorRuntimeSettings) -> QdrantCaseStore:
    return QdrantCaseStore(qdrant_url=str(settings.qdrant_url))


def _build_bootstrap_snapshot() -> StrategyConfigSnapshot:
    generated_at = datetime.now(UTC)
    return StrategyConfigSnapshot(
        version_id="bootstrap",
        generated_at=generated_at,
        effective_from=generated_at,
        expires_at=generated_at + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.5,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="bootstrap",
        ttl_sec=300,
    )


def build_governor_runtime() -> GovernorRuntime:
    settings = GovernorRuntimeSettings()
    return GovernorRuntime(
        settings=settings,
        service=build_governor_service(),
        governor_client=build_governor_client(settings),
        case_store=build_case_store(settings),
        snapshot_store=build_snapshot_store(settings),
        runtime_store=build_runtime_state_store(settings),
        history_store=build_history_store(settings),
        last_snapshot=_build_bootstrap_snapshot(),
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _wait_for_next_cycle(delay_sec: int) -> None:
    await asyncio.sleep(delay_sec)


async def _run_governor_cycle(runtime: GovernorRuntime) -> None:
    state_summary = runtime.service.build_state_summary(
        runtime_store=runtime.runtime_store,
        snapshot_store=runtime.snapshot_store,
        history_store=runtime.history_store,
        symbols=runtime.last_snapshot.symbol_whitelist,
        fallback_snapshot=runtime.last_snapshot,
    )
    trigger_reason = runtime.service.determine_trigger_reason(
        state_summary,
        latest_snapshot=runtime.last_snapshot,
        now=datetime.now(UTC),
    )
    governance_cases = runtime.case_store.search_governance_cases(
        runtime.service.build_governance_case_query(
            state_summary,
            trigger_reason=trigger_reason,
        )
    )
    state_summary = {
        **state_summary,
        "trigger_reason": trigger_reason,
        "governance_cases": governance_cases,
    }
    for opinion in state_summary.get("expert_opinions", []):
        if isinstance(opinion, dict):
            runtime.history_store.append_expert_opinion(opinion)

    cycle_status = "published"

    def _publish_snapshot(snapshot: StrategyConfigSnapshot) -> None:
        runtime.snapshot_store.set_latest_snapshot(snapshot.version_id, snapshot)
        runtime.published_snapshots.append(snapshot)
        runtime.history_store.append_strategy_snapshot(
            {
                "version_id": snapshot.version_id,
                "market_mode": snapshot.market_mode.value,
                "approval_state": snapshot.approval_state.value,
            }
        )
        runtime.history_store.append_governor_run(
            {
                "version_id": snapshot.version_id,
                "status": cycle_status,
            }
        )

    result = await runtime.service.run_cycle(
        state_summary=state_summary,
        last_snapshot=runtime.last_snapshot,
        governor_client=runtime.governor_client,
        publish_snapshot=_publish_snapshot,
    )
    cycle_status = result.status
    runtime.last_snapshot = result.snapshot
    runtime.consecutive_failures = 0 if result.status == "published" else runtime.consecutive_failures + 1
    runtime.runtime_store.set_governor_health_summary(
        runtime.service.build_health_summary(
            snapshot=runtime.last_snapshot,
            trigger_reason=trigger_reason,
            status=result.status,
            consecutive_failures=runtime.consecutive_failures,
        )
    )


async def _run_governor_loop(runtime: GovernorRuntime) -> None:
    while True:
        await _run_governor_cycle(runtime)
        state_summary = runtime.service.build_state_summary(
            runtime_store=runtime.runtime_store,
            snapshot_store=runtime.snapshot_store,
            history_store=runtime.history_store,
            symbols=runtime.last_snapshot.symbol_whitelist,
            fallback_snapshot=runtime.last_snapshot,
        )
        trigger_reason = runtime.service.determine_trigger_reason(
            state_summary,
            latest_snapshot=runtime.last_snapshot,
            now=datetime.now(UTC),
        )
        delay_sec = 0 if trigger_reason != "schedule" else runtime.settings.governor_interval_sec
        await _wait_for_next_cycle(delay_sec)


async def _run_governor(runtime: GovernorRuntime) -> None:
    await _run_governor_loop(runtime)


def main() -> int:
    asyncio.run(_run_governor(build_governor_runtime()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

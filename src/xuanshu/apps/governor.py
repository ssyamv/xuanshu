from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from xuanshu.config.settings import GovernorRuntimeSettings
from xuanshu.contracts.research import ResearchTrigger, StrategyPackage
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.governor.research_providers import create_research_provider
from xuanshu.governor.service import GovernorService
from xuanshu.governor.research import StrategyResearchEngine
from xuanshu.infra.ai.governor_client import ConfiguredGovernorAgentRunner, GovernorClient
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.infra.storage.qdrant_store import QdrantCaseStore
from xuanshu.infra.storage.redis_store import (
    RedisRuntimeStateStore,
    RedisSnapshotStore,
    RuntimeStateStore,
    SnapshotStore,
)

_APPROVED_RESEARCH_SOURCE_REASON = "approved research package"


@dataclass(slots=True)
class GovernorRuntime:
    settings: GovernorRuntimeSettings
    service: GovernorService
    research_engine: StrategyResearchEngine
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


def build_research_engine(settings: GovernorRuntimeSettings) -> StrategyResearchEngine:
    provider = create_research_provider(
        provider_name=settings.research_provider,
        openai_api_key=settings.openai_api_key,
        timeout_sec=settings.ai_timeout_sec,
    )
    return StrategyResearchEngine(provider=provider)


def build_snapshot_store(settings: GovernorRuntimeSettings) -> SnapshotStore:
    return RedisSnapshotStore(redis_url=str(settings.redis_url))


def build_runtime_state_store(settings: GovernorRuntimeSettings) -> RuntimeStateStore:
    return RedisRuntimeStateStore(redis_url=str(settings.redis_url))


def build_history_store(settings: GovernorRuntimeSettings) -> PostgresRuntimeStore:
    return PostgresRuntimeStore(dsn=str(settings.postgres_dsn))


def build_case_store(settings: GovernorRuntimeSettings) -> QdrantCaseStore:
    return QdrantCaseStore(qdrant_url=str(settings.qdrant_url))


async def _build_research_candidates(
    runtime: GovernorRuntime,
    state_summary: dict[str, object],
) -> list[StrategyPackage]:
    symbol_summaries = state_summary.get("symbol_summaries")
    if not isinstance(symbol_summaries, list) or not symbol_summaries:
        return []

    symbol_scope: list[str] = []
    for summary in symbol_summaries:
        if not isinstance(summary, dict):
            continue
        symbol = summary.get("symbol")
        if isinstance(symbol, str) and symbol not in symbol_scope:
            symbol_scope.append(symbol)
    if not symbol_scope:
        return []

    historical_rows = _load_research_historical_rows(runtime, symbol_scope)
    if not historical_rows:
        return []

    return [
        await runtime.research_engine.build_candidate_package_from_provider(
            trigger=ResearchTrigger.SCHEDULE,
            symbol_scope=symbol_scope,
            market_environment="trend",
            historical_rows=historical_rows,
            research_reason="governor strategy research",
        )
    ]


def _load_research_historical_rows(
    runtime: GovernorRuntime,
    symbol_scope: list[str],
) -> list[dict[str, object]]:
    historical_rows: list[dict[str, object]] = []
    for table in ("orders", "fills", "positions"):
        for row in reversed(runtime.history_store.list_recent_rows(table, limit=20)):
            historical_row = _coerce_historical_row(row, symbol_scope)
            if historical_row is not None:
                historical_rows.append(historical_row)
    return historical_rows


def _coerce_historical_row(
    row: object,
    symbol_scope: list[str],
) -> dict[str, object] | None:
    if not isinstance(row, dict):
        return None
    symbol = row.get("symbol")
    if not isinstance(symbol, str) or symbol not in symbol_scope:
        return None
    close = _coerce_historical_close(
        row.get("mark_price"),
        row.get("price"),
        row.get("average_price"),
    )
    if close is None:
        return None
    timestamp = _coerce_historical_timestamp(
        row.get("generated_at") or row.get("timestamp") or row.get("created_at")
    )
    if timestamp is None:
        return None
    return {
        "timestamp": timestamp,
        "close": close,
    }


def _coerce_historical_close(*values: object) -> float | None:
    for value in values:
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                continue
            try:
                return float(normalized)
            except ValueError:
                continue
    return None


def _coerce_historical_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    return None


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
        research_engine=build_research_engine(settings),
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

    research_provider = runtime.research_engine.provider.provider_name.value
    research_status = "skipped"
    research_provider_success: bool | None = None
    research_error: str | None = None
    research_candidates: list[StrategyPackage] = []
    try:
        research_candidates = await _build_research_candidates(runtime, state_summary)
        if research_candidates:
            research_status = "succeeded"
            research_provider_success = True
    except Exception as exc:
        research_status = "failed"
        research_provider_success = False
        research_error = str(exc)
        research_candidates = []

    approved_research_candidates: list[StrategyPackage] = []
    if research_candidates:
        expert_opinions = runtime.service.build_expert_opinions(
            state_summary,
            now=datetime.now(UTC),
        )
        committee_summary = runtime.service.build_committee_summary(
            expert_opinions,
            research_candidates=research_candidates,
        )
        approved_candidate_ids = committee_summary.get("approved_research_candidates")
        if isinstance(approved_candidate_ids, list):
            approved_candidate_ids = {
                candidate_id for candidate_id in approved_candidate_ids if isinstance(candidate_id, str)
            }
            approved_research_candidates = [
                candidate for candidate in research_candidates if candidate.strategy_package_id in approved_candidate_ids
            ]
        state_summary = {
            **state_summary,
            "committee_summary": committee_summary,
        }
        if approved_research_candidates:
            state_summary["research_candidates"] = [
                candidate.model_dump(mode="json") for candidate in approved_research_candidates
            ]

    published_snapshot: StrategyConfigSnapshot | None = None
    approved_research_candidate_ids: list[str] = []
    committee_summary = state_summary.get("committee_summary")
    if isinstance(committee_summary, dict):
        approved_candidates = committee_summary.get("approved_research_candidates")
        if isinstance(approved_candidates, list):
            approved_research_candidate_ids = [
                candidate_id for candidate_id in approved_candidates if isinstance(candidate_id, str)
            ]

    def _publish_snapshot(snapshot: StrategyConfigSnapshot) -> None:
        nonlocal published_snapshot
        if snapshot.approval_state == ApprovalState.APPROVED and approved_research_candidate_ids:
            snapshot = snapshot.model_copy(update={"source_reason": _APPROVED_RESEARCH_SOURCE_REASON})
        published_snapshot = snapshot
        runtime.snapshot_store.set_latest_snapshot(snapshot.version_id, snapshot)
        runtime.published_snapshots.append(snapshot)
        runtime.history_store.append_strategy_snapshot(
            {
                "version_id": snapshot.version_id,
                "market_mode": snapshot.market_mode.value,
                "approval_state": snapshot.approval_state.value,
            }
        )

    result = await runtime.service.run_cycle(
        state_summary=state_summary,
        last_snapshot=runtime.last_snapshot,
        governor_client=runtime.governor_client,
        publish_snapshot=_publish_snapshot,
    )
    runtime.last_snapshot = published_snapshot or result.snapshot
    runtime.history_store.append_governor_run(
        {
            "version_id": runtime.last_snapshot.version_id,
            "status": result.status,
            "research_provider": research_provider,
            "research_status": research_status,
            "research_provider_success": research_provider_success,
            "research_error": research_error,
            "research_candidate_count": len(research_candidates),
            "approved_research_candidate_ids": approved_research_candidate_ids,
        }
    )
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

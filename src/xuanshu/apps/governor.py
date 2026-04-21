from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xuanshu.config.settings import GovernorRuntimeSettings
from xuanshu.contracts.approval import ApprovalDecision, ApprovalRecord
from xuanshu.contracts.backtest import BacktestReport
from xuanshu.contracts.research import ResearchTrigger, StrategyPackage
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.governor.backtest import BacktestValidator
from xuanshu.governor.research_providers import ResearchProviderName, create_research_provider
from xuanshu.governor.service import GovernorCycleResult, GovernorService, _APPROVED_RESEARCH_SOURCE_REASON
from xuanshu.governor.research import StrategyResearchEngine
from xuanshu.infra.okx.rest import OkxRestClient
from xuanshu.infra.ai.governor_client import (
    CodexCliGovernorAgentRunner,
    ConfiguredGovernorAgentRunner,
    GovernorClient,
)
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.infra.storage.qdrant_store import QdrantCaseStore
from xuanshu.infra.storage.redis_store import (
    RedisRuntimeStateStore,
    RedisSnapshotStore,
    RuntimeStateStore,
    SnapshotStore,
)
from xuanshu.ops.runtime_logging import configure_runtime_logger

_LOGGER = configure_runtime_logger("xuanshu.governor")
_OKX_REST_BASE_URL = "https://www.okx.com"
_MIN_RESEARCH_NET_PNL = 0.5
_SEARCH_MODE_ACTIVE = "search_until_qualified"
_SEARCH_MODE_OBSERVE = "observe_until_invalidated"
_SEARCH_RETRY_DELAY_SEC = 5


@dataclass(slots=True)
class GovernorRuntime:
    settings: GovernorRuntimeSettings
    service: GovernorService
    research_engine: StrategyResearchEngine
    backtest_validator: BacktestValidator
    governor_client: GovernorClient
    market_data_client: OkxRestClient
    case_store: QdrantCaseStore
    snapshot_store: SnapshotStore
    runtime_store: RuntimeStateStore
    history_store: PostgresRuntimeStore
    last_snapshot: StrategyConfigSnapshot
    published_snapshots: list[StrategyConfigSnapshot] = field(default_factory=list)
    consecutive_failures: int = 0


@dataclass(slots=True)
class ResearchCandidateBuildResult:
    status: str
    historical_rows: list[dict[str, object]] = field(default_factory=list)
    candidates: list[StrategyPackage] = field(default_factory=list)
    candidate_historical_rows: dict[str, list[dict[str, object]]] = field(default_factory=dict)


_RESEARCH_MARKET_ENVIRONMENTS = ("trend", "range", "mean_reversion")


def build_governor_service() -> GovernorService:
    return GovernorService()


def build_governor_client(settings: GovernorRuntimeSettings) -> GovernorClient:
    if settings.research_provider == ResearchProviderName.CODEX_CLI:
        return GovernorClient(agent_runner=CodexCliGovernorAgentRunner())
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


def build_backtest_validator() -> BacktestValidator:
    return BacktestValidator()


def build_market_data_client(settings: GovernorRuntimeSettings) -> OkxRestClient:
    return OkxRestClient(
        base_url=_OKX_REST_BASE_URL,
        api_key="public",
        timeout=float(settings.ai_timeout_sec),
    )


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
    return (await _prepare_research_candidates(runtime, state_summary)).candidates


async def _prepare_research_candidates(
    runtime: GovernorRuntime,
    state_summary: dict[str, object],
) -> ResearchCandidateBuildResult:
    symbol_summaries = state_summary.get("symbol_summaries")
    if not isinstance(symbol_summaries, list) or not symbol_summaries:
        return ResearchCandidateBuildResult(status="missing_symbol_summaries")

    symbol_scope: list[str] = []
    for summary in symbol_summaries:
        if not isinstance(summary, dict):
            continue
        symbol = summary.get("symbol")
        if isinstance(symbol, str) and symbol not in symbol_scope:
            symbol_scope.append(symbol)
    if not symbol_scope:
        return ResearchCandidateBuildResult(status="missing_symbol_summaries")

    all_candidates: list[StrategyPackage] = []
    candidate_historical_rows: dict[str, list[dict[str, object]]] = {}
    first_historical_rows: list[dict[str, object]] = []
    for symbol in symbol_scope:
        candidate_scope = [symbol]
        candidate_rows = await _load_research_historical_rows(runtime, candidate_scope)
        if not candidate_rows:
            continue
        if not first_historical_rows:
            first_historical_rows = candidate_rows
        for market_environment in _RESEARCH_MARKET_ENVIRONMENTS:
            candidate_packages = await runtime.research_engine.build_candidate_packages_from_provider(
                trigger=ResearchTrigger.SCHEDULE,
                symbol_scope=candidate_scope,
                market_environment=market_environment,
                historical_rows=candidate_rows,
                research_reason="governor strategy research",
            )
            for candidate in candidate_packages:
                all_candidates.append(candidate)
                candidate_historical_rows[candidate.strategy_package_id] = candidate_rows

    if not all_candidates:
        return ResearchCandidateBuildResult(status="insufficient_history")

    return ResearchCandidateBuildResult(
        status="candidate_built",
        historical_rows=first_historical_rows,
        candidates=all_candidates,
        candidate_historical_rows=candidate_historical_rows,
    )


async def _load_research_historical_rows(
    runtime: GovernorRuntime,
    symbol_scope: list[str],
) -> list[dict[str, object]]:
    if len(symbol_scope) != 1:
        return []
    return await _fetch_okx_historical_rows(runtime, symbol_scope[0])


async def _fetch_okx_historical_rows(
    runtime: GovernorRuntime,
    symbol: str,
) -> list[dict[str, object]]:
    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(days=runtime.settings.research_history_days)
    after: str | None = None
    historical_rows: list[dict[str, object]] = []

    while True:
        candles = await runtime.market_data_client.fetch_history_candles(
            symbol,
            bar=runtime.settings.research_bar,
            after=after,
            limit=100,
        )
        if not candles:
            break
        normalized_batch = [_coerce_okx_candle_row(item) for item in candles]
        normalized_batch = [row for row in normalized_batch if row is not None]
        if not normalized_batch:
            break

        historical_rows.extend(
            row for row in normalized_batch if row["timestamp"] >= window_start
        )
        oldest_timestamp = min(row["timestamp"] for row in normalized_batch)
        if oldest_timestamp <= window_start:
            break
        oldest_ts_ms = int(oldest_timestamp.timestamp() * 1000)
        after = str(oldest_ts_ms)
        await asyncio.sleep(0.25)

    historical_rows.sort(key=lambda row: row["timestamp"])
    if not historical_rows:
        return []
    return historical_rows


def _coerce_okx_candle_row(row: object) -> dict[str, object] | None:
    if not isinstance(row, dict):
        return None
    timestamp = _coerce_okx_timestamp(row.get("ts"))
    close = _coerce_historical_close(row.get("close"))
    if timestamp is None or close is None:
        return None
    return {
        "timestamp": timestamp,
        "close": close,
    }


def _coerce_okx_timestamp(value: object) -> datetime | None:
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            return datetime.fromtimestamp(int(normalized) / 1000, tz=UTC)
        except ValueError:
            return None
    if isinstance(value, int | float | Decimal):
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    return None


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


def _build_bootstrap_snapshot(settings: GovernorRuntimeSettings) -> StrategyConfigSnapshot:
    generated_at = datetime.now(UTC)
    return StrategyConfigSnapshot(
        version_id="bootstrap",
        generated_at=generated_at,
        effective_from=generated_at,
        expires_at=generated_at + timedelta(minutes=5),
        symbol_whitelist=list(settings.okx_symbols),
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
        backtest_validator=build_backtest_validator(),
        governor_client=build_governor_client(settings),
        market_data_client=build_market_data_client(settings),
        case_store=build_case_store(settings),
        snapshot_store=build_snapshot_store(settings),
        runtime_store=build_runtime_state_store(settings),
        history_store=build_history_store(settings),
        last_snapshot=_build_bootstrap_snapshot(settings),
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _wait_for_next_cycle(delay_sec: int) -> None:
    await asyncio.sleep(delay_sec)


async def _run_governor_cycle(runtime: GovernorRuntime) -> None:
    current_search_mode = _get_strategy_search_mode(runtime)
    state_summary = runtime.service.build_state_summary(
        runtime_store=runtime.runtime_store,
        snapshot_store=runtime.snapshot_store,
        history_store=runtime.history_store,
        symbols=runtime.settings.okx_symbols,
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
        "strategy_search_mode": current_search_mode,
    }
    for opinion in state_summary.get("expert_opinions", []):
        if isinstance(opinion, dict):
            runtime.history_store.append_expert_opinion(opinion)

    research_provider = runtime.research_engine.provider.provider_name.value
    research_status = "missing_symbol_summaries"
    research_provider_success: bool | None = None
    research_error: str | None = None
    validation_status = "not_requested"
    validation_error: str | None = None
    approval_status = "not_requested"
    approval_error: str | None = None
    approval_record: ApprovalRecord | None = None
    backtest_report_id: str | None = None
    approval_record_id: str | None = None
    research_candidates: list[StrategyPackage] = []
    historical_rows: list[dict[str, object]] = []
    candidate_historical_rows: dict[str, list[dict[str, object]]] = {}
    try:
        research_result = await _prepare_research_candidates(runtime, state_summary)
        research_status = research_result.status
        historical_rows = research_result.historical_rows
        research_candidates = research_result.candidates
        candidate_historical_rows = research_result.candidate_historical_rows
        if research_candidates:
            research_provider_success = True
    except Exception as exc:
        research_status = "failed"
        research_provider_success = False
        research_error = str(exc)
        research_candidates = []
        historical_rows = []

    approved_research_candidates: list[StrategyPackage] = []
    if current_search_mode == _SEARCH_MODE_OBSERVE and trigger_reason == "schedule":
        runtime.runtime_store.set_strategy_search_mode(_SEARCH_MODE_OBSERVE)
        runtime.history_store.append_governor_run(
            {
                "version_id": runtime.last_snapshot.version_id,
                "status": "observing",
                "error": None,
                "research_provider": research_provider,
                "research_status": "idle",
                "research_provider_success": None,
                "research_error": None,
                "research_candidate_count": 0,
                "approved_research_candidate_ids": [],
                "validation_status": "not_requested",
                "validation_error": None,
                "approval_status": "not_requested",
                "approval_error": None,
                "backtest_report_id": None,
                "approval_record_id": None,
            }
        )
        runtime.runtime_store.set_governor_health_summary(
            runtime.service.build_health_summary(
                snapshot=runtime.last_snapshot,
                trigger_reason=trigger_reason,
                status="observing",
                consecutive_failures=runtime.consecutive_failures,
            )
        )
        _LOGGER.info(
            "cycle_completed",
            extra={
                "service": "governor",
                "trigger_reason": trigger_reason,
                "status": "observing",
                "error": None,
                "snapshot_version": runtime.last_snapshot.version_id,
                "market_mode": runtime.last_snapshot.market_mode.value,
                "research_status": "idle",
                "research_candidate_count": 0,
                "approved_research_candidate_count": 0,
                "consecutive_failures": runtime.consecutive_failures,
            },
        )
        return
    if current_search_mode == _SEARCH_MODE_OBSERVE and trigger_reason != "schedule":
        current_search_mode = _SEARCH_MODE_ACTIVE
        runtime.runtime_store.set_strategy_search_mode(current_search_mode)
    if research_candidates:
        validated_candidates: list[tuple[StrategyPackage, BacktestReport]] = []
        best_report_under_threshold: tuple[StrategyPackage, BacktestReport] | None = None
        candidate_validation_errors: list[str] = []
        for candidate in research_candidates:
            if not runtime.history_store.has_strategy_package(
                strategy_package_id=candidate.strategy_package_id,
            ):
                runtime.history_store.append_strategy_package(candidate.model_dump(mode="json"))
            try:
                backtest_report = runtime.backtest_validator.validate(
                    package=candidate,
                    historical_rows=candidate_historical_rows.get(candidate.strategy_package_id, historical_rows),
                )
            except Exception as exc:
                candidate_validation_errors.append(str(exc))
                continue
            if not runtime.history_store.has_backtest_report(
                backtest_report_id=backtest_report.backtest_report_id,
            ):
                runtime.history_store.append_backtest_report(backtest_report.model_dump(mode="json"))
            if backtest_report.net_pnl >= _MIN_RESEARCH_NET_PNL:
                validated_candidates.append((candidate, backtest_report))
            elif (
                best_report_under_threshold is None
                or backtest_report.net_pnl > best_report_under_threshold[1].net_pnl
            ):
                best_report_under_threshold = (candidate, backtest_report)

        if validated_candidates or best_report_under_threshold is not None:
            validated_candidates.sort(key=lambda item: item[1].net_pnl, reverse=True)
            if not validated_candidates:
                validation_status = "failed"
                approval_status = "validation_failed"
                if best_report_under_threshold is not None:
                    candidate, backtest_report = best_report_under_threshold
                    validation_error = (
                        f"best candidate net_pnl {backtest_report.net_pnl} "
                        f"did not exceed minimum quality threshold {_MIN_RESEARCH_NET_PNL}"
                    )
                    runtime.runtime_store.set_pending_approval_summary(
                        {
                            "pending_count": 0,
                            "latest_strategy_package_id": candidate.strategy_package_id,
                            "approval_status": approval_status,
                        }
                    )
                    runtime.runtime_store.set_backtest_health_summary(
                        {
                            "status": "candidate_rejected_low_quality",
                            "candidate_strategy_package_id": candidate.strategy_package_id,
                            "candidate_backtest_report_id": backtest_report.backtest_report_id,
                            "minimum_net_pnl": _MIN_RESEARCH_NET_PNL,
                            "best_net_pnl": backtest_report.net_pnl,
                        }
                    )
                state_summary["validation_status"] = validation_status
                state_summary["validation_error"] = validation_error
            else:
                research_candidates = [candidate for candidate, _report in validated_candidates]
                expert_opinions = runtime.service.build_expert_opinions(
                    state_summary,
                    now=datetime.now(UTC),
                )
                committee_summary = runtime.service.build_committee_summary(
                    expert_opinions,
                    research_candidates=research_candidates,
                )
                state_summary = {
                    **state_summary,
                    "committee_summary": committee_summary,
                }

                candidate, backtest_report = validated_candidates[0]
                validation_status = "succeeded"
                backtest_report_id = backtest_report.backtest_report_id
                baseline_report = _build_baseline_backtest_report(
                    runtime=runtime,
                    historical_rows=candidate_historical_rows.get(candidate.strategy_package_id, historical_rows),
                )
                if baseline_report is not None and backtest_report.net_pnl <= baseline_report.net_pnl:
                    validation_status = "failed"
                    validation_error = (
                        f"candidate net_pnl {backtest_report.net_pnl} "
                        f"did not exceed current strategy {baseline_report.net_pnl}"
                    )
                    approval_status = "validation_failed"
                    runtime.runtime_store.set_pending_approval_summary(
                        {
                            "pending_count": 0,
                            "latest_strategy_package_id": candidate.strategy_package_id,
                            "approval_status": approval_status,
                        }
                    )
                    state_summary["validation_status"] = validation_status
                    state_summary["validation_error"] = validation_error
                    state_summary["baseline_backtest_report"] = baseline_report.model_dump(mode="json")
                    runtime.runtime_store.set_backtest_health_summary(
                        {
                            "status": "candidate_rejected_underperforming_baseline",
                            "candidate_strategy_package_id": candidate.strategy_package_id,
                            "candidate_backtest_report_id": backtest_report.backtest_report_id,
                        }
                    )
                    backtest_report_id = None
                else:
                    if baseline_report is not None:
                        state_summary["baseline_backtest_report"] = baseline_report.model_dump(mode="json")
                    approval_record = runtime.service.build_auto_approval_record(
                        state_summary=state_summary,
                        strategy_package_id=candidate.strategy_package_id,
                        backtest_report_id=backtest_report.backtest_report_id,
                        created_at=datetime.now(UTC),
                    )
                    approval_status = approval_record.decision.value
                    approval_record_id = approval_record.approval_record_id
                    existing_approval_record = runtime.history_store.find_approval_record(
                        strategy_package_id=candidate.strategy_package_id,
                        backtest_report_id=backtest_report.backtest_report_id,
                    )
                    if existing_approval_record is None or existing_approval_record.get("approval_record_id") != approval_record.approval_record_id:
                        runtime.history_store.append_approval_record(approval_record.model_dump(mode="json"))
                    state_summary["approval_required"] = True
                    state_summary["approval_record"] = approval_record.model_dump(mode="json")
                    runtime.runtime_store.set_pending_approval_summary(
                        {
                            "pending_count": 0,
                            "latest_strategy_package_id": candidate.strategy_package_id,
                            "latest_backtest_report_id": backtest_report.backtest_report_id,
                            "approval_status": approval_status,
                        }
                    )
                    if approval_record.decision in {
                        ApprovalDecision.APPROVED,
                        ApprovalDecision.APPROVED_WITH_GUARDRAILS,
                    }:
                        approved_research_candidates = [candidate]
                        runtime.runtime_store.set_strategy_search_mode(_SEARCH_MODE_OBSERVE)
                        state_summary["research_candidates"] = [candidate.model_dump(mode="json")]
                        state_summary["approved_source_reason"] = _APPROVED_RESEARCH_SOURCE_REASON
                        runtime.runtime_store.set_latest_approved_package_summary(
                            {
                                "latest_strategy_package_id": candidate.strategy_package_id,
                                "backtest_report_id": backtest_report.backtest_report_id,
                                "approval_record_id": approval_record.approval_record_id,
                                "approved_at": approval_record.created_at.isoformat().replace("+00:00", "Z"),
                                "approval_decision": approval_record.decision.value,
                            }
                        )

                state_summary["validation_status"] = validation_status
                state_summary["validation_error"] = validation_error
        elif candidate_validation_errors:
            validation_status = "failed"
            validation_error = candidate_validation_errors[0]
            approval_status = "validation_failed"
            runtime.runtime_store.set_pending_approval_summary(
                {
                    "pending_count": 0,
                    "latest_strategy_package_id": research_candidates[-1].strategy_package_id,
                    "approval_status": approval_status,
                }
            )
            state_summary["validation_status"] = validation_status
            state_summary["validation_error"] = validation_error

    published_snapshot: StrategyConfigSnapshot | None = None
    approved_research_candidate_ids = [
        candidate.strategy_package_id for candidate in approved_research_candidates
    ]

    def _publish_snapshot(snapshot: StrategyConfigSnapshot) -> None:
        nonlocal published_snapshot
        published_snapshot = snapshot
        runtime.snapshot_store.set_latest_snapshot(snapshot.version_id, snapshot)
        runtime.published_snapshots.append(snapshot)
        payload = {
            "version_id": snapshot.version_id,
            "market_mode": snapshot.market_mode.value,
            "approval_state": snapshot.approval_state.value,
            "symbol_whitelist": list(snapshot.symbol_whitelist),
            "strategy_enable_flags": dict(snapshot.strategy_enable_flags),
            "risk_multiplier": snapshot.risk_multiplier,
            "per_symbol_max_position": snapshot.per_symbol_max_position,
            "max_leverage": snapshot.max_leverage,
            "source_reason": snapshot.source_reason,
        }
        if approved_research_candidates and backtest_report_id is not None and approval_record is not None:
            payload.update(
                {
                    "strategy_package_id": approved_research_candidates[0].strategy_package_id,
                    "backtest_report_id": backtest_report_id,
                    "approval_record_id": approval_record.approval_record_id,
                    "approval_decision": approval_record.decision.value,
                    "guardrails": approval_record.guardrails,
                }
            )
        runtime.history_store.append_strategy_snapshot(payload)

    if trigger_reason == "schedule" and research_status != "candidate_built":
        result = GovernorCycleResult(
            snapshot=runtime.last_snapshot,
            status="unchanged",
            error=research_error,
        )
    else:
        result = await runtime.service.run_cycle(
            state_summary=state_summary,
            last_snapshot=runtime.last_snapshot,
            governor_client=runtime.governor_client,
            publish_snapshot=_publish_snapshot,
            trigger_reason=trigger_reason,
        )
    runtime.last_snapshot = published_snapshot or result.snapshot
    if not approved_research_candidate_ids:
        runtime.runtime_store.set_strategy_search_mode(_SEARCH_MODE_ACTIVE)
    runtime.history_store.append_governor_run(
        {
            "version_id": runtime.last_snapshot.version_id,
            "status": result.status,
            "error": result.error,
            "research_provider": research_provider,
            "research_status": research_status,
            "research_provider_success": research_provider_success,
            "research_error": research_error,
            "research_candidate_count": len(research_candidates),
            "approved_research_candidate_ids": approved_research_candidate_ids,
            "validation_status": validation_status,
            "validation_error": validation_error,
            "approval_status": approval_status,
            "approval_error": approval_error,
            "backtest_report_id": backtest_report_id,
            "approval_record_id": approval_record_id,
        }
    )
    runtime.consecutive_failures = 0 if result.status in {
        "published",
        "unchanged",
        "approval_pending",
        "approval_rejected",
        "approval_needs_revision",
        "approval_invalid",
        "validation_failed",
    } else runtime.consecutive_failures + 1
    runtime.runtime_store.set_governor_health_summary(
        runtime.service.build_health_summary(
            snapshot=runtime.last_snapshot,
            trigger_reason=trigger_reason,
            status=result.status,
            consecutive_failures=runtime.consecutive_failures,
        )
    )
    _LOGGER.info(
        "cycle_completed",
        extra={
            "service": "governor",
            "trigger_reason": trigger_reason,
            "status": result.status,
            "error": result.error,
            "snapshot_version": runtime.last_snapshot.version_id,
            "market_mode": runtime.last_snapshot.market_mode.value,
            "research_status": research_status,
            "research_candidate_count": len(research_candidates),
            "approved_research_candidate_count": len(approved_research_candidate_ids),
            "consecutive_failures": runtime.consecutive_failures,
        },
    )


async def _run_governor_loop(runtime: GovernorRuntime) -> None:
    while True:
        await _run_governor_cycle(runtime)
        current_search_mode = _get_strategy_search_mode(runtime)
        state_summary = runtime.service.build_state_summary(
            runtime_store=runtime.runtime_store,
            snapshot_store=runtime.snapshot_store,
            history_store=runtime.history_store,
            symbols=runtime.settings.okx_symbols,
            fallback_snapshot=runtime.last_snapshot,
        )
        trigger_reason = runtime.service.determine_trigger_reason(
            state_summary,
            latest_snapshot=runtime.last_snapshot,
            now=datetime.now(UTC),
        )
        if current_search_mode == _SEARCH_MODE_ACTIVE:
            delay_sec = 0 if trigger_reason != "schedule" else _SEARCH_RETRY_DELAY_SEC
        else:
            delay_sec = 0 if trigger_reason != "schedule" else runtime.settings.governor_interval_sec
        await _wait_for_next_cycle(delay_sec)


def _get_strategy_search_mode(runtime: GovernorRuntime) -> str:
    latest_runs = runtime.history_store.list_recent_rows("governor_runs", limit=1)
    if latest_runs:
        latest_run = latest_runs[0]
        approved_ids = latest_run.get("approved_research_candidate_ids")
        status = latest_run.get("status")
        if isinstance(approved_ids, list) and approved_ids and status == "published":
            runtime.runtime_store.set_strategy_search_mode(_SEARCH_MODE_OBSERVE)
            return _SEARCH_MODE_OBSERVE
        if status in {
            "validation_failed",
            "approval_rejected",
            "approval_pending",
            "approval_needs_revision",
            "approval_invalid",
            "frozen",
        } and (not isinstance(approved_ids, list) or not approved_ids):
            runtime.runtime_store.set_strategy_search_mode(_SEARCH_MODE_ACTIVE)
            return _SEARCH_MODE_ACTIVE
    current = runtime.runtime_store.get_strategy_search_mode()
    if current in {_SEARCH_MODE_ACTIVE, _SEARCH_MODE_OBSERVE}:
        return current
    latest_approved = runtime.runtime_store.get_latest_approved_package_summary()
    if isinstance(latest_approved, dict) and isinstance(latest_approved.get("latest_strategy_package_id"), str):
        runtime.runtime_store.set_strategy_search_mode(_SEARCH_MODE_OBSERVE)
        return _SEARCH_MODE_OBSERVE
    runtime.runtime_store.set_strategy_search_mode(_SEARCH_MODE_ACTIVE)
    return _SEARCH_MODE_ACTIVE


def _build_baseline_backtest_report(
    *,
    runtime: GovernorRuntime,
    historical_rows: list[dict[str, object]],
) -> BacktestReport | None:
    if not _snapshot_has_tradable_strategy(runtime.last_snapshot):
        return _build_zero_trade_backtest_report(
            strategy_package_id=f"baseline-{runtime.last_snapshot.version_id}",
            strategy_def_id=f"baseline-{runtime.last_snapshot.version_id}",
            symbol_scope=list(runtime.last_snapshot.symbol_whitelist),
            historical_rows=historical_rows,
        )
    current_snapshot_row = runtime.history_store.find_strategy_snapshot(version_id=runtime.last_snapshot.version_id)
    if current_snapshot_row is None:
        return None
    strategy_package_id = current_snapshot_row.get("strategy_package_id")
    if not isinstance(strategy_package_id, str) or not strategy_package_id.strip():
        return None
    strategy_package_row = runtime.history_store.find_strategy_package(strategy_package_id=strategy_package_id)
    if strategy_package_row is None:
        return None
    baseline_package = StrategyPackage.model_validate(strategy_package_row)
    return runtime.backtest_validator.validate(
        package=baseline_package,
        historical_rows=historical_rows,
    )


def _snapshot_has_tradable_strategy(snapshot: StrategyConfigSnapshot) -> bool:
    return bool(
        snapshot.strategy_enable_flags.get("breakout") is True
        or snapshot.strategy_enable_flags.get("mean_reversion") is True
    )


def _build_zero_trade_backtest_report(
    *,
    strategy_package_id: str,
    strategy_def_id: str,
    symbol_scope: list[str],
    historical_rows: list[dict[str, object]],
) -> BacktestReport:
    normalized_rows = sorted(
        historical_rows,
        key=lambda row: row["timestamp"],
    )
    timestamps = [row["timestamp"].astimezone(UTC) for row in normalized_rows]
    return BacktestReport(
        backtest_report_id=f"{strategy_package_id}-zero-baseline",
        strategy_package_id=strategy_package_id,
        strategy_def_id=strategy_def_id,
        symbol_scope=symbol_scope,
        dataset_range={
            "start": timestamps[0],
            "end": timestamps[-1],
            "regime_fit": "unknown",
        },
        sample_count=len(historical_rows),
        trade_count=0,
        trade_count_sufficiency="insufficient",
        net_pnl=0.0,
        return_percent=0.0,
        max_drawdown=0.0,
        win_rate=0.0,
        profit_factor=0.0,
        stability_score=0.0,
        overfit_risk="high",
        failure_modes=[],
        invalidating_conditions=[],
        generated_at=timestamps[-1],
    )


async def _run_governor(runtime: GovernorRuntime) -> None:
    await _run_governor_loop(runtime)


def main() -> int:
    asyncio.run(_run_governor(build_governor_runtime()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

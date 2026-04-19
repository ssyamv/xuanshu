from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from xuanshu.contracts.governance import ExpertOpinion
from xuanshu.contracts.research import StrategyPackage
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.infra.ai.governor_client import GovernorClient


class GovernorRuntimeStateReader(Protocol):
    def get_run_mode(self) -> RunMode | None:
        ...

    def get_fault_flags(self) -> dict[str, object] | None:
        ...

    def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
        ...


class GovernorSnapshotReader(Protocol):
    def get_latest_snapshot(self) -> StrategyConfigSnapshot | None:
        ...


class GovernorHistoryReader(Protocol):
    def list_recent_rows(self, table: str, limit: int = 10) -> list[dict[str, object]]:
        ...


_RUN_MODE_PRIORITY = {
    RunMode.NORMAL: 0,
    RunMode.DEGRADED: 1,
    RunMode.REDUCE_ONLY: 2,
    RunMode.HALTED: 3,
}
_FAILURE_DEGRADED_THRESHOLD = 3


@dataclass(frozen=True, slots=True)
class GovernorCycleResult:
    snapshot: StrategyConfigSnapshot
    status: str


class GovernorService:
    def freeze_on_failure(self, last_snapshot: StrategyConfigSnapshot) -> StrategyConfigSnapshot:
        return last_snapshot.model_copy(deep=True)

    def build_state_summary(
        self,
        *,
        runtime_store: GovernorRuntimeStateReader,
        snapshot_store: GovernorSnapshotReader,
        history_store: GovernorHistoryReader,
        symbols: tuple[str, ...] | list[str],
        fallback_snapshot: StrategyConfigSnapshot | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        current_mode = runtime_store.get_run_mode()
        latest_snapshot = snapshot_store.get_latest_snapshot() or fallback_snapshot
        fault_flags = runtime_store.get_fault_flags() or {}
        symbol_summaries = [
            summary
            for symbol in symbols
            if (summary := runtime_store.get_symbol_runtime_summary(symbol)) is not None
        ]
        recent_risk_events = history_store.list_recent_rows("risk_events", limit=5)
        recent_governor_runs = history_store.list_recent_rows("governor_runs", limit=5)
        summary = {
            "scope": "governor",
            "current_run_mode": current_mode.value if current_mode is not None else "unknown",
            "latest_snapshot_version": latest_snapshot.version_id if latest_snapshot is not None else "unknown",
            "active_fault_flags": sorted(fault_flags),
            "symbol_summaries": symbol_summaries,
            "recent_risk_events": recent_risk_events,
            "recent_governor_runs": recent_governor_runs,
        }
        expert_opinions = self.build_expert_opinions(summary, now=timestamp)
        summary["expert_opinions"] = [self._serialize_expert_opinion(opinion) for opinion in expert_opinions]
        summary["committee_summary"] = self.build_committee_summary(expert_opinions)
        return summary

    def build_expert_opinions(
        self,
        state_summary: Mapping[str, object],
        *,
        now: datetime,
    ) -> list[ExpertOpinion]:
        timestamp = now.astimezone(UTC)
        snapshot_version = str(state_summary.get("latest_snapshot_version", "unknown"))
        symbol_scope = self._extract_symbol_scope(state_summary)
        active_fault_flags = self._coerce_string_list(state_summary.get("active_fault_flags"))
        recent_risk_events = self._coerce_mapping_list(state_summary.get("recent_risk_events"))
        current_mode = str(state_summary.get("current_run_mode", "unknown"))
        recognized_current_mode = (
            current_mode if current_mode in RunMode._value2member_map_ else RunMode.NORMAL.value
        )

        market_supporting_facts = [f"symbols={len(symbol_scope)}"]
        market_risk_flags: list[str] = []
        if active_fault_flags:
            market_supporting_facts.insert(0, f"fault_flags={','.join(active_fault_flags)}")
            market_risk_flags = [f"fault:{flag}" for flag in active_fault_flags]
            market_decision = "fragmented_market_structure"
            market_confidence = 0.7
        elif symbol_scope == ["SYSTEM"]:
            market_decision = "insufficient_market_context"
            market_confidence = 0.5
        else:
            market_decision = "stable_market_structure"
            market_confidence = 0.6

        risk_supporting_facts: list[str] = []
        risk_flags: list[str] = []
        if recent_risk_events:
            risk_supporting_facts.append(f"risk_events={len(recent_risk_events)}")
            risk_flags.extend(
                f"event:{event_type}"
                for event in recent_risk_events
                if isinstance((event_type := event.get("event_type")), str)
            )
        if recognized_current_mode != RunMode.NORMAL.value:
            risk_supporting_facts.append(f"current_run_mode={recognized_current_mode}")
            risk_flags.append(f"mode:{recognized_current_mode}")
        if recent_risk_events or recognized_current_mode != RunMode.NORMAL.value:
            risk_decision = "tighten_risk"
            risk_confidence = 0.9 if recent_risk_events else 0.7
        else:
            risk_decision = "maintain_risk"
            risk_confidence = 0.55
            risk_supporting_facts.append("current_run_mode=normal")

        event_supporting_facts: list[str] = []
        event_risk_flags: list[str] = []
        if recent_risk_events:
            event_supporting_facts.append(f"recent_risk_events={len(recent_risk_events)}")
            event_risk_flags.extend(
                f"event:{event_type}"
                for event in recent_risk_events
                if isinstance((event_type := event.get("event_type")), str)
            )
            event_decision = "block_event_driven_risk"
            event_confidence = 0.85
        elif active_fault_flags:
            event_supporting_facts.append(f"active_fault_flags={len(active_fault_flags)}")
            event_risk_flags.extend(f"fault:{flag}" for flag in active_fault_flags)
            event_decision = "filter_fault_window"
            event_confidence = 0.75
        else:
            event_supporting_facts.append("recent_risk_events=0")
            event_decision = "allow_scheduled_risk"
            event_confidence = 0.6

        return [
            ExpertOpinion(
                opinion_id=f"market_structure:{snapshot_version}:{timestamp.strftime('%Y%m%d%H%M%S')}",
                expert_type="market_structure",
                generated_at=timestamp,
                symbol_scope=symbol_scope,
                decision=market_decision,
                confidence=market_confidence,
                supporting_facts=market_supporting_facts,
                risk_flags=market_risk_flags,
                ttl_sec=300,
            ),
            ExpertOpinion(
                opinion_id=f"risk:{snapshot_version}:{timestamp.strftime('%Y%m%d%H%M%S')}",
                expert_type="risk",
                generated_at=timestamp,
                symbol_scope=symbol_scope,
                decision=risk_decision,
                confidence=risk_confidence,
                supporting_facts=risk_supporting_facts,
                risk_flags=risk_flags,
                ttl_sec=300,
            ),
            ExpertOpinion(
                opinion_id=f"event_filter:{snapshot_version}:{timestamp.strftime('%Y%m%d%H%M%S')}",
                expert_type="event_filter",
                generated_at=timestamp,
                symbol_scope=symbol_scope,
                decision=event_decision,
                confidence=event_confidence,
                supporting_facts=event_supporting_facts,
                risk_flags=event_risk_flags,
                ttl_sec=300,
            ),
        ]

    def build_committee_summary(
        self,
        expert_opinions: list[ExpertOpinion],
        *,
        research_candidates: list[StrategyPackage] | None = None,
    ) -> dict[str, object]:
        research_candidates = research_candidates or []
        blocking_flags = sorted({flag for opinion in expert_opinions for flag in opinion.risk_flags})
        if any(flag in {"event:recovery_failed", "fault:manual_takeover", "mode:halted"} for flag in blocking_flags):
            recommended_mode_floor = RunMode.HALTED.value
        elif "mode:reduce_only" in blocking_flags:
            recommended_mode_floor = RunMode.REDUCE_ONLY.value
        elif blocking_flags:
            recommended_mode_floor = RunMode.DEGRADED.value
        else:
            recommended_mode_floor = RunMode.NORMAL.value

        benign_decisions = {
            "stable_market_structure",
            "positioned_market_structure",
            "insufficient_market_context",
            "maintain_risk",
            "allow_scheduled_risk",
        }
        consensus_decision = (
            "maintain"
            if all(opinion.decision in benign_decisions for opinion in expert_opinions)
            else "tighten_risk"
        )
        summary: dict[str, object] = {
            "consensus_decision": consensus_decision,
            "recommended_mode_floor": recommended_mode_floor,
            "blocking_flags": blocking_flags,
            "requires_human_review": any(
                flag in {"event:recovery_failed", "fault:manual_takeover", "mode:halted"}
                for flag in blocking_flags
            ),
            "active_experts": [opinion.expert_type for opinion in expert_opinions],
        }
        if research_candidates:
            summary["research_candidate_count"] = len(research_candidates)
            summary["approved_research_candidates"] = (
                [candidate.strategy_package_id for candidate in research_candidates]
                if self._committee_can_approve_research(
                    expert_opinions=expert_opinions,
                    blocking_flags=blocking_flags,
                    consensus_decision=consensus_decision,
                    requires_human_review=bool(summary["requires_human_review"]),
                )
                else []
            )
        return summary

    @staticmethod
    def _committee_can_approve_research(
        *,
        expert_opinions: list[ExpertOpinion],
        blocking_flags: list[str],
        consensus_decision: str,
        requires_human_review: bool,
    ) -> bool:
        if not expert_opinions:
            return True
        return not blocking_flags and consensus_decision == "maintain" and not requires_human_review

    def build_governance_case_query(
        self,
        state_summary: Mapping[str, object],
        *,
        trigger_reason: str,
    ) -> dict[str, object]:
        committee_summary = (
            state_summary.get("committee_summary")
            if isinstance(state_summary.get("committee_summary"), Mapping)
            else {}
        )
        return {
            "trigger_reason": trigger_reason,
            "current_run_mode": state_summary.get("current_run_mode", "unknown"),
            "recommended_mode_floor": committee_summary.get(
                "recommended_mode_floor",
                RunMode.NORMAL.value,
            ),
            "active_fault_flags": self._coerce_string_list(state_summary.get("active_fault_flags")),
        }

    def apply_guardrails(
        self,
        candidate: StrategyConfigSnapshot,
        state_summary: Mapping[str, object],
    ) -> StrategyConfigSnapshot:
        current_run_mode_value = state_summary.get("current_run_mode")
        current_run_mode = (
            RunMode(current_run_mode_value)
            if isinstance(current_run_mode_value, str) and current_run_mode_value in RunMode._value2member_map_
            else None
        )
        active_fault_flags = state_summary.get("active_fault_flags", [])
        if not isinstance(active_fault_flags, list):
            active_fault_flags = []
        recent_risk_events = state_summary.get("recent_risk_events", [])
        if not isinstance(recent_risk_events, list):
            recent_risk_events = []

        symbol_summaries = state_summary.get("symbol_summaries", [])
        observed_symbols: list[str] = []
        if isinstance(symbol_summaries, list):
            for item in symbol_summaries:
                if isinstance(item, Mapping):
                    symbol = item.get("symbol")
                    if isinstance(symbol, str) and symbol not in observed_symbols:
                        observed_symbols.append(symbol)

        market_mode = candidate.market_mode
        if active_fault_flags and _RUN_MODE_PRIORITY[market_mode] < _RUN_MODE_PRIORITY[RunMode.DEGRADED]:
            market_mode = RunMode.DEGRADED
        if current_run_mode is not None and _RUN_MODE_PRIORITY[market_mode] < _RUN_MODE_PRIORITY[current_run_mode]:
            market_mode = current_run_mode

        risk_multiplier = candidate.risk_multiplier
        if recent_risk_events:
            risk_multiplier = min(risk_multiplier, 0.25)
            if _RUN_MODE_PRIORITY[market_mode] < _RUN_MODE_PRIORITY[RunMode.DEGRADED]:
                market_mode = RunMode.DEGRADED

        symbol_whitelist = list(candidate.symbol_whitelist)
        for symbol in observed_symbols:
            if symbol not in symbol_whitelist:
                symbol_whitelist.append(symbol)

        approval_state = candidate.approval_state
        if any(
            isinstance(item, Mapping) and item.get("event_type") == "recovery_failed"
            for item in recent_risk_events
        ):
            approval_state = ApprovalState.PENDING
            market_mode = RunMode.HALTED
            risk_multiplier = 0.0

        source_reason = candidate.source_reason
        if source_reason.endswith("|guardrailed"):
            updated_reason = source_reason
        else:
            updated_reason = f"{source_reason}|guardrailed"

        return candidate.model_copy(
            update={
                "market_mode": market_mode,
                "risk_multiplier": risk_multiplier,
                "approval_state": approval_state,
                "symbol_whitelist": symbol_whitelist,
                "source_reason": updated_reason,
            }
        )

    def build_health_summary(
        self,
        *,
        snapshot: StrategyConfigSnapshot,
        trigger_reason: str,
        status: str,
        consecutive_failures: int,
    ) -> dict[str, object]:
        market_mode = snapshot.market_mode
        health_state = "healthy"
        if status == "frozen" and consecutive_failures >= _FAILURE_DEGRADED_THRESHOLD:
            health_state = "degraded"
            if _RUN_MODE_PRIORITY[market_mode] < _RUN_MODE_PRIORITY[RunMode.DEGRADED]:
                market_mode = RunMode.DEGRADED
        return {
            "status": status,
            "trigger": trigger_reason,
            "snapshot_version": snapshot.version_id,
            "market_mode": market_mode.value,
            "approval_state": snapshot.approval_state.value,
            "risk_multiplier": snapshot.risk_multiplier,
            "consecutive_failures": consecutive_failures,
            "health_state": health_state,
        }

    def determine_trigger_reason(
        self,
        state_summary: Mapping[str, object],
        *,
        latest_snapshot: StrategyConfigSnapshot | None,
        now: datetime,
    ) -> str:
        now = now.astimezone(UTC)
        recent_risk_events = state_summary.get("recent_risk_events", [])
        if isinstance(recent_risk_events, list) and recent_risk_events:
            return "risk_event"

        current_run_mode_value = state_summary.get("current_run_mode")
        if (
            isinstance(current_run_mode_value, str)
            and current_run_mode_value in RunMode._value2member_map_
            and RunMode(current_run_mode_value) != RunMode.NORMAL
        ):
            return "mode_change"

        if latest_snapshot is not None and latest_snapshot.expires_at <= now + timedelta(minutes=1):
            return "snapshot_expiring"

        return "schedule"

    async def run_cycle(
        self,
        state_summary: Mapping[str, object],
        last_snapshot: StrategyConfigSnapshot,
        governor_client: GovernorClient,
        publish_snapshot: Callable[[StrategyConfigSnapshot], None],
    ) -> GovernorCycleResult:
        try:
            snapshot = self.apply_guardrails(
                await governor_client.generate_snapshot(state_summary),
                state_summary,
            )
            status = "published"
        except Exception:
            snapshot = self.freeze_on_failure(last_snapshot)
            status = "frozen"

        publish_snapshot(snapshot)
        return GovernorCycleResult(snapshot=snapshot, status=status)

    def _extract_symbol_scope(self, state_summary: Mapping[str, object]) -> list[str]:
        symbol_scope: list[str] = []
        symbol_summaries = state_summary.get("symbol_summaries")
        if isinstance(symbol_summaries, list):
            for summary in symbol_summaries:
                if not isinstance(summary, Mapping):
                    continue
                symbol = summary.get("symbol")
                if isinstance(symbol, str) and symbol not in symbol_scope:
                    symbol_scope.append(symbol)
        return symbol_scope or ["SYSTEM"]

    def _coerce_string_list(self, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    def _coerce_mapping_list(self, value: object) -> list[Mapping[str, object]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, Mapping)]

    def _serialize_expert_opinion(self, opinion: ExpertOpinion) -> dict[str, object]:
        payload = opinion.model_dump(mode="json")
        generated_at = opinion.generated_at.isoformat()
        payload["generated_at"] = generated_at
        return payload

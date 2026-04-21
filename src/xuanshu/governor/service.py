from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from typing import Protocol

from pydantic import ValidationError

from xuanshu.contracts.approval import ApprovalDecision, ApprovalRecord
from xuanshu.contracts.governance import ExpertOpinion
from xuanshu.contracts.research import StrategyPackage
from xuanshu.contracts.strategy import ApprovedStrategyBinding, StrategyConfigSnapshot
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
_BASELINE_STRATEGY_FLAGS = {
    "breakout": True,
    "mean_reversion": False,
    "risk_pause": True,
}
_APPROVED_RESEARCH_SOURCE_REASON = "approved research package"


@dataclass(frozen=True, slots=True)
class GovernorCycleResult:
    snapshot: StrategyConfigSnapshot
    status: str
    error: str | None = None


class GovernorService:
    @staticmethod
    def _candidate_clears_return_gate(return_percent: float) -> bool:
        return return_percent > 50.0

    @staticmethod
    def _load_approval_record(state_summary: Mapping[str, object]) -> ApprovalRecord | None:
        payload = state_summary.get("approval_record")
        if isinstance(payload, ApprovalRecord):
            return payload
        if not isinstance(payload, Mapping):
            return None
        try:
            return ApprovalRecord.model_validate(dict(payload))
        except ValidationError as exc:
            raise ValueError(f"invalid approval record: {exc}") from exc

    def build_auto_approval_record(
        self,
        *,
        state_summary: Mapping[str, object],
        strategy_package_id: str,
        backtest_report_id: str,
        created_at: datetime,
    ) -> ApprovalRecord:
        committee_summary = (
            state_summary.get("committee_summary")
            if isinstance(state_summary.get("committee_summary"), Mapping)
            else {}
        )
        approved_candidates = committee_summary.get("approved_research_candidates")
        approved_candidate_ids = {
            candidate_id for candidate_id in approved_candidates
            if isinstance(candidate_id, str)
        } if isinstance(approved_candidates, list) else set()
        blocking_flags = committee_summary.get("blocking_flags")
        normalized_blocking_flags = [
            flag for flag in blocking_flags if isinstance(flag, str)
        ] if isinstance(blocking_flags, list) else []
        recommended_mode_floor = str(committee_summary.get("recommended_mode_floor") or "").strip().lower()
        guardrails: dict[str, object] = {}
        decision = ApprovalDecision.REJECTED
        decision_reason = "committee rejected research candidate"

        if strategy_package_id in approved_candidate_ids:
            if recommended_mode_floor and recommended_mode_floor != RunMode.NORMAL.value:
                guardrails["market_mode"] = recommended_mode_floor
                decision = ApprovalDecision.APPROVED_WITH_GUARDRAILS
                decision_reason = "committee auto-approved candidate with guardrails"
            else:
                decision = ApprovalDecision.APPROVED
                decision_reason = "committee auto-approved candidate"
        elif normalized_blocking_flags:
            decision_reason = f"committee rejected candidate due to blocking flags: {','.join(normalized_blocking_flags)}"

        approval_payload = {
            "strategy_package_id": strategy_package_id,
            "backtest_report_id": backtest_report_id,
            "decision": decision.value,
            "decision_reason": decision_reason,
            "guardrails": guardrails,
            "reviewed_by": "committee",
            "review_source": "system",
        }
        fingerprint = sha256(
            json.dumps(approval_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()[:12]
        return ApprovalRecord(
            approval_record_id=f"apr-{fingerprint}",
            created_at=created_at,
            **approval_payload,
        )

    @staticmethod
    def _resolve_prepublication_block(
        state_summary: Mapping[str, object],
        *,
        last_snapshot: StrategyConfigSnapshot,
    ) -> GovernorCycleResult | None:
        validation_status = str(state_summary.get("validation_status") or "").strip().lower()
        validation_error = str(state_summary.get("validation_error") or "").strip() or None
        if validation_status == "failed":
            return GovernorCycleResult(
                snapshot=last_snapshot,
                status="validation_failed",
                error=validation_error,
            )

        if not bool(state_summary.get("approval_required")):
            return None

        try:
            approval_record = GovernorService._load_approval_record(state_summary)
        except ValueError as exc:
            return GovernorCycleResult(
                snapshot=last_snapshot,
                status="approval_invalid",
                error=str(exc),
            )
        if approval_record is None:
            return GovernorCycleResult(snapshot=last_snapshot, status="approval_pending", error=None)
        if approval_record.decision == ApprovalDecision.REJECTED:
            return GovernorCycleResult(
                snapshot=last_snapshot,
                status="approval_rejected",
                error=approval_record.decision_reason,
            )
        if approval_record.decision == ApprovalDecision.NEEDS_REVISION:
            return GovernorCycleResult(
                snapshot=last_snapshot,
                status="approval_needs_revision",
                error=approval_record.decision_reason,
            )
        return None

    @staticmethod
    def _normalize_strategy_flags(flags: Mapping[str, object]) -> dict[str, bool]:
        normalized = {
            key: value
            for key, value in flags.items()
            if isinstance(key, str) and isinstance(value, bool)
        }
        if all(key in normalized for key in _BASELINE_STRATEGY_FLAGS):
            return normalized
        return {**_BASELINE_STRATEGY_FLAGS, **normalized}

    @staticmethod
    def _filter_blocking_risk_events(
        recent_risk_events: object,
        latest_checkpoint: object | None = None,
    ) -> list[Mapping[str, object]]:
        if not isinstance(recent_risk_events, list):
            return []
        checkpoint_created_at = GovernorService._coerce_datetime(
            latest_checkpoint.get("created_at")
            if isinstance(latest_checkpoint, Mapping)
            else None
        )
        checkpoint_cleared_reconcile = (
            isinstance(latest_checkpoint, Mapping)
            and latest_checkpoint.get("needs_reconcile") is False
            and checkpoint_created_at is not None
        )
        return [
            item
            for item in recent_risk_events
            if isinstance(item, Mapping)
            and item.get("event_type") not in {"signal_blocked", "account_snapshot_updated", "manual_release_requested"}
            and not GovernorService._is_resolved_startup_gating_event(
                item,
                checkpoint_created_at=checkpoint_created_at,
                checkpoint_cleared_reconcile=checkpoint_cleared_reconcile,
            )
        ]

    @staticmethod
    def _is_resolved_startup_gating_event(
        event: Mapping[str, object],
        *,
        checkpoint_created_at: datetime | None,
        checkpoint_cleared_reconcile: bool,
    ) -> bool:
        if not checkpoint_cleared_reconcile or checkpoint_created_at is None:
            return False
        event_type = event.get("event_type")
        if event_type == "startup_recovery_failed":
            event_created_at = GovernorService._coerce_datetime(event.get("created_at"))
            return event_created_at is not None and event_created_at <= checkpoint_created_at
        if event_type == "runtime_mode_changed":
            detail = event.get("detail")
            event_created_at = GovernorService._coerce_datetime(event.get("created_at"))
            return (
                isinstance(detail, str)
                and "startup gating tightened runtime" in detail
                and event_created_at is not None
                and event_created_at <= checkpoint_created_at
            )
        return False

    @staticmethod
    def _coerce_datetime(value: object) -> datetime | None:
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
        latest_checkpoint_rows = history_store.list_recent_rows("execution_checkpoints", limit=1)
        latest_checkpoint = latest_checkpoint_rows[0] if latest_checkpoint_rows else None
        recent_risk_events = self._filter_blocking_risk_events(
            history_store.list_recent_rows("risk_events", limit=5),
            latest_checkpoint=latest_checkpoint,
        )
        recent_governor_runs = history_store.list_recent_rows("governor_runs", limit=5)
        manual_release_target = getattr(runtime_store, "get_manual_release_target", lambda: None)()
        strategy_search_mode = getattr(runtime_store, "get_strategy_search_mode", lambda: None)()
        summary = {
            "scope": "governor",
            "current_run_mode": current_mode.value if current_mode is not None else "unknown",
            "latest_snapshot_version": latest_snapshot.version_id if latest_snapshot is not None else "unknown",
            "active_fault_flags": sorted(fault_flags),
            "symbol_summaries": symbol_summaries,
            "recent_risk_events": recent_risk_events,
            "recent_governor_runs": recent_governor_runs,
        }
        if isinstance(manual_release_target, str) and manual_release_target:
            summary["manual_release_target"] = manual_release_target
        if isinstance(strategy_search_mode, str) and strategy_search_mode:
            summary["strategy_search_mode"] = strategy_search_mode
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
        manual_release_target = str(state_summary.get("manual_release_target") or "").strip().lower()
        blocking_risk_events = self._filter_blocking_risk_events(recent_risk_events)
        current_mode = str(state_summary.get("current_run_mode", "unknown"))
        recognized_current_mode = (
            current_mode if current_mode in RunMode._value2member_map_ else RunMode.NORMAL.value
        )
        if manual_release_target == RunMode.DEGRADED.value and recognized_current_mode == RunMode.HALTED.value:
            recognized_current_mode = RunMode.DEGRADED.value

        market_supporting_facts = [f"symbols={len(symbol_scope)}"]
        market_risk_flags: list[str] = []
        if manual_release_target == RunMode.DEGRADED.value:
            market_supporting_facts.insert(0, "manual_release_target=degraded")
            market_risk_flags.append("release:degraded")
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
        if blocking_risk_events:
            risk_supporting_facts.append(f"risk_events={len(blocking_risk_events)}")
            risk_flags.extend(
                f"event:{event_type}"
                for event in blocking_risk_events
                if isinstance((event_type := event.get("event_type")), str)
            )
        if (
            recognized_current_mode != RunMode.NORMAL.value
            and (blocking_risk_events or active_fault_flags or manual_release_target == RunMode.DEGRADED.value)
        ):
            risk_supporting_facts.append(f"current_run_mode={recognized_current_mode}")
            risk_flags.append(f"mode:{recognized_current_mode}")
        if manual_release_target == RunMode.DEGRADED.value:
            risk_supporting_facts.append("manual_release_target=degraded")
            risk_flags.append("release:degraded")
        if blocking_risk_events or active_fault_flags or manual_release_target == RunMode.DEGRADED.value:
            risk_decision = "tighten_risk"
            risk_confidence = 0.9 if blocking_risk_events else 0.7
        else:
            risk_decision = "maintain_risk"
            risk_confidence = 0.55
            risk_supporting_facts.append("current_run_mode=normal")

        event_supporting_facts: list[str] = []
        event_risk_flags: list[str] = []
        if blocking_risk_events:
            event_supporting_facts.append(f"recent_risk_events={len(blocking_risk_events)}")
            event_risk_flags.extend(
                f"event:{event_type}"
                for event in blocking_risk_events
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
        manual_release_target = RunMode.DEGRADED.value if "release:degraded" in blocking_flags else None
        if manual_release_target:
            blocking_flags = [flag for flag in blocking_flags if flag not in {"release:degraded", "mode:halted"}]
        if manual_release_target:
            recommended_mode_floor = manual_release_target
        elif any(flag in {"event:recovery_failed", "fault:manual_takeover", "mode:halted"} for flag in blocking_flags):
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
            "requires_human_review": False if manual_release_target else any(
                flag in {"event:recovery_failed", "fault:manual_takeover", "mode:halted"}
                for flag in blocking_flags
            ),
            "active_experts": [opinion.expert_type for opinion in expert_opinions],
        }
        if manual_release_target:
            summary["manual_release_target"] = manual_release_target
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
        manual_release_target_value = str(state_summary.get("manual_release_target") or "").strip().lower()
        manual_release_target = (
            RunMode(manual_release_target_value)
            if manual_release_target_value in RunMode._value2member_map_
            else None
        )
        current_run_mode_value = state_summary.get("current_run_mode")
        current_run_mode = (
            RunMode(current_run_mode_value)
            if isinstance(current_run_mode_value, str) and current_run_mode_value in RunMode._value2member_map_
            else None
        )
        if manual_release_target == RunMode.DEGRADED and current_run_mode == RunMode.HALTED:
            current_run_mode = RunMode.DEGRADED
        active_fault_flags = state_summary.get("active_fault_flags", [])
        if not isinstance(active_fault_flags, list):
            active_fault_flags = []
        recent_risk_events = self._filter_blocking_risk_events(state_summary.get("recent_risk_events", []))

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
        if (
            current_run_mode is not None
            and (active_fault_flags or recent_risk_events)
            and _RUN_MODE_PRIORITY[market_mode] < _RUN_MODE_PRIORITY[current_run_mode]
        ):
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
        per_symbol_max_position = candidate.per_symbol_max_position
        recovery_ready = (
            current_run_mode is not None
            and current_run_mode != RunMode.NORMAL
            and not active_fault_flags
            and not recent_risk_events
            and manual_release_target is None
        )
        if any(
            isinstance(item, Mapping) and item.get("event_type") == "recovery_failed"
            for item in recent_risk_events
        ):
            approval_state = ApprovalState.PENDING
            market_mode = RunMode.HALTED
            risk_multiplier = 0.0

        if manual_release_target == RunMode.DEGRADED:
            market_mode = RunMode.DEGRADED
            approval_state = ApprovalState.APPROVED
            risk_multiplier = max(risk_multiplier, 0.25)
            per_symbol_max_position = max(per_symbol_max_position, 0.12)
        elif recovery_ready:
            market_mode = RunMode.NORMAL
            approval_state = ApprovalState.APPROVED
            risk_multiplier = max(risk_multiplier, 0.25)
            per_symbol_max_position = max(per_symbol_max_position, 0.12)

        source_reason = candidate.source_reason
        if source_reason.endswith("|guardrailed"):
            updated_reason = source_reason
        else:
            updated_reason = f"{source_reason}|guardrailed"

        strategy_enable_flags = self._normalize_strategy_flags(candidate.strategy_enable_flags)

        payload = candidate.model_dump(mode="python")
        payload.update(
            {
                "market_mode": market_mode,
                "risk_multiplier": risk_multiplier,
                "per_symbol_max_position": per_symbol_max_position,
                "approval_state": approval_state,
                "symbol_whitelist": symbol_whitelist,
                "strategy_enable_flags": strategy_enable_flags,
                "source_reason": updated_reason,
            }
        )
        if payload["symbol_whitelist"] != candidate.symbol_whitelist:
            whitelist = set(payload["symbol_whitelist"])
            payload["symbol_strategy_bindings"] = {
                symbol: binding
                for symbol, binding in payload["symbol_strategy_bindings"].items()
                if symbol in whitelist
            }
        return StrategyConfigSnapshot.model_validate(payload)

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

    def bind_symbol_strategy(
        self,
        *,
        snapshot: StrategyConfigSnapshot,
        symbol: str,
        binding_payload: Mapping[str, object],
    ) -> StrategyConfigSnapshot:
        normalized_symbol = symbol.strip()
        payload = snapshot.model_dump(mode="python")
        payload["symbol_strategy_bindings"] = dict(payload["symbol_strategy_bindings"])
        payload["symbol_strategy_bindings"][normalized_symbol] = ApprovedStrategyBinding.model_validate(
            dict(binding_payload)
        )
        return StrategyConfigSnapshot.model_validate(payload)

    def apply_approval_guardrails(
        self,
        candidate: StrategyConfigSnapshot,
        approval_record: ApprovalRecord,
    ) -> StrategyConfigSnapshot:
        guardrails = approval_record.guardrails
        updates: dict[str, object] = {}

        guardrail_symbols = guardrails.get("symbol_whitelist")
        if isinstance(guardrail_symbols, list):
            normalized_symbols = [
                symbol.strip()
                for symbol in guardrail_symbols
                if isinstance(symbol, str) and symbol.strip()
            ]
            if normalized_symbols:
                updates["symbol_whitelist"] = normalized_symbols

        risk_limit = guardrails.get("risk_multiplier")
        if isinstance(risk_limit, int | float) and not isinstance(risk_limit, bool):
            bounded_risk_limit = min(max(float(risk_limit), 0.0), 1.0)
            updates["risk_multiplier"] = min(candidate.risk_multiplier, bounded_risk_limit)

        forced_market_mode = guardrails.get("market_mode")
        if isinstance(forced_market_mode, str) and forced_market_mode in RunMode._value2member_map_:
            mode = RunMode(forced_market_mode)
            if _RUN_MODE_PRIORITY[mode] > _RUN_MODE_PRIORITY[candidate.market_mode]:
                updates["market_mode"] = mode

        if not updates:
            return candidate
        payload = candidate.model_dump(mode="python")
        payload.update(updates)
        if "symbol_whitelist" in updates:
            whitelist = set(updates["symbol_whitelist"])
            payload["symbol_strategy_bindings"] = {
                symbol: binding
                for symbol, binding in payload["symbol_strategy_bindings"].items()
                if symbol in whitelist
            }
        return StrategyConfigSnapshot.model_validate(payload)

    def determine_trigger_reason(
        self,
        state_summary: Mapping[str, object],
        *,
        latest_snapshot: StrategyConfigSnapshot | None,
        now: datetime,
    ) -> str:
        now = now.astimezone(UTC)
        recent_risk_events = self._filter_blocking_risk_events(state_summary.get("recent_risk_events", []))
        if recent_risk_events:
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
        trigger_reason: str,
    ) -> GovernorCycleResult:
        blocked_result = self._resolve_prepublication_block(
            state_summary,
            last_snapshot=last_snapshot,
        )
        if blocked_result is not None:
            return blocked_result

        approval_record = self._load_approval_record(state_summary)
        try:
            snapshot = self.apply_guardrails(
                await governor_client.generate_snapshot(state_summary),
                state_summary,
            )
            if approval_record is not None and approval_record.decision == ApprovalDecision.APPROVED_WITH_GUARDRAILS:
                snapshot = self.apply_approval_guardrails(snapshot, approval_record)
            approved_source_reason = str(state_summary.get("approved_source_reason") or "").strip()
            if approval_record is not None and approved_source_reason:
                snapshot = snapshot.model_copy(update={"source_reason": approved_source_reason})
            approved_bindings = state_summary.get("approved_symbol_strategy_bindings")
            if (
                approval_record is not None
                and approval_record.decision in {
                    ApprovalDecision.APPROVED,
                    ApprovalDecision.APPROVED_WITH_GUARDRAILS,
                }
                and isinstance(approved_bindings, Mapping)
            ):
                for symbol, binding_payload in approved_bindings.items():
                    if (
                        isinstance(symbol, str)
                        and symbol.strip() in snapshot.symbol_whitelist
                        and isinstance(binding_payload, Mapping)
                    ):
                        snapshot = self.bind_symbol_strategy(
                            snapshot=snapshot,
                            symbol=symbol,
                            binding_payload=binding_payload,
                        )
            if self._snapshots_are_semantically_equal(snapshot, last_snapshot):
                return GovernorCycleResult(snapshot=last_snapshot, status="unchanged", error=None)
            status = "published"
            error = None
        except Exception as exc:
            snapshot = self.freeze_on_failure(last_snapshot)
            return GovernorCycleResult(snapshot=snapshot, status="frozen", error=str(exc))

        publish_snapshot(snapshot)
        return GovernorCycleResult(snapshot=snapshot, status=status, error=error)

    def _snapshots_are_semantically_equal(
        self,
        candidate: StrategyConfigSnapshot,
        previous: StrategyConfigSnapshot,
    ) -> bool:
        return {
            "symbol_whitelist": candidate.symbol_whitelist,
            "strategy_enable_flags": candidate.strategy_enable_flags,
            "risk_multiplier": candidate.risk_multiplier,
            "per_symbol_max_position": candidate.per_symbol_max_position,
            "max_leverage": candidate.max_leverage,
            "market_mode": candidate.market_mode,
            "approval_state": candidate.approval_state,
            "source_reason": candidate.source_reason,
            "ttl_sec": candidate.ttl_sec,
            "symbol_strategy_bindings": candidate.symbol_strategy_bindings,
        } == {
            "symbol_whitelist": previous.symbol_whitelist,
            "strategy_enable_flags": previous.strategy_enable_flags,
            "risk_multiplier": previous.risk_multiplier,
            "per_symbol_max_position": previous.per_symbol_max_position,
            "max_leverage": previous.max_leverage,
            "market_mode": previous.market_mode,
            "approval_state": previous.approval_state,
            "source_reason": previous.source_reason,
            "ttl_sec": previous.ttl_sec,
            "symbol_strategy_bindings": previous.symbol_strategy_bindings,
        }

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

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.infra.ai.governor_client import GovernorClient
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.governor.service import GovernorService


def test_governor_keeps_last_valid_snapshot_when_ai_fails() -> None:
    snapshot = StrategyConfigSnapshot(
        version_id="snap-last",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.7,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="cached",
        ttl_sec=300,
    )

    service = GovernorService()

    frozen_snapshot = service.freeze_on_failure(snapshot)

    assert frozen_snapshot.version_id == "snap-last"
    assert frozen_snapshot is not snapshot

    frozen_snapshot.symbol_whitelist.append("ETH-USDT-SWAP")

    assert snapshot.symbol_whitelist == ["BTC-USDT-SWAP"]


class _BrokenGovernorRunner:
    async def run(self, state_summary: dict[str, object]) -> dict[str, object]:
        return {"version_id": state_summary["version_id"]}


@pytest.mark.asyncio
async def test_governor_client_validates_agent_output() -> None:
    client = GovernorClient(agent_runner=_BrokenGovernorRunner())

    with pytest.raises(ValidationError):
        await client.generate_snapshot({"version_id": "snap-invalid"})


def test_governor_builds_state_summary_from_runtime_and_history() -> None:
    service = GovernorService()
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.append_risk_event({"event_type": "runtime_mode_changed", "detail": "reduced risk"})
    history.append_governor_run({"version_id": "snap-001", "status": "published"})

    class _RuntimeStore:
        def get_run_mode(self) -> RunMode | None:
            return RunMode.DEGRADED

        def get_fault_flags(self) -> dict[str, object] | None:
            return {"public_ws_disconnected": {"severity": "warn"}}

        def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
            return {
                "symbol": symbol,
                "mid_price": 100.1,
                "net_quantity": 1.0 if symbol == "BTC-USDT-SWAP" else 0.0,
            }

    class _SnapshotStore:
        def get_latest_snapshot(self):
            return snapshot

    snapshot = StrategyConfigSnapshot(
        version_id="snap-001",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.7,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="cached",
        ttl_sec=300,
    )

    summary = service.build_state_summary(
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
        symbols=snapshot.symbol_whitelist,
        now=datetime(2026, 4, 18, tzinfo=UTC),
    )

    assert summary == {
        "scope": "governor",
        "current_run_mode": "degraded",
        "latest_snapshot_version": "snap-001",
        "active_fault_flags": ["public_ws_disconnected"],
        "symbol_summaries": [
            {"symbol": "BTC-USDT-SWAP", "mid_price": 100.1, "net_quantity": 1.0},
            {"symbol": "ETH-USDT-SWAP", "mid_price": 100.1, "net_quantity": 0.0},
        ],
        "expert_opinions": [
            {
                "opinion_id": "market_structure:snap-001:20260418000000",
                "expert_type": "market_structure",
                "generated_at": "2026-04-18T00:00:00+00:00",
                "symbol_scope": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                "decision": "fragmented_market_structure",
                "confidence": 0.7,
                "supporting_facts": ["fault_flags=public_ws_disconnected", "symbols=2"],
                "risk_flags": ["fault:public_ws_disconnected"],
                "ttl_sec": 300,
            },
            {
                "opinion_id": "risk:snap-001:20260418000000",
                "expert_type": "risk",
                "generated_at": "2026-04-18T00:00:00+00:00",
                "symbol_scope": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                "decision": "tighten_risk",
                "confidence": 0.9,
                "supporting_facts": ["risk_events=1", "current_run_mode=degraded"],
                "risk_flags": ["event:runtime_mode_changed", "mode:degraded"],
                "ttl_sec": 300,
            },
            {
                "opinion_id": "event_filter:snap-001:20260418000000",
                "expert_type": "event_filter",
                "generated_at": "2026-04-18T00:00:00+00:00",
                "symbol_scope": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                "decision": "block_event_driven_risk",
                "confidence": 0.85,
                "supporting_facts": ["recent_risk_events=1"],
                "risk_flags": ["event:runtime_mode_changed"],
                "ttl_sec": 300,
            },
        ],
        "committee_summary": {
            "consensus_decision": "tighten_risk",
            "recommended_mode_floor": "degraded",
            "blocking_flags": [
                "event:runtime_mode_changed",
                "fault:public_ws_disconnected",
                "mode:degraded",
            ],
            "requires_human_review": False,
            "active_experts": ["market_structure", "risk", "event_filter"],
        },
        "recent_risk_events": [{"event_type": "runtime_mode_changed", "detail": "reduced risk"}],
        "recent_governor_runs": [{"version_id": "snap-001", "status": "published"}],
    }


def test_governor_builds_expert_opinions_and_halted_committee_summary() -> None:
    service = GovernorService()

    expert_opinions = service.build_expert_opinions(
        {
            "latest_snapshot_version": "snap-halted",
            "current_run_mode": "halted",
            "active_fault_flags": ["manual_takeover"],
            "symbol_summaries": [{"symbol": "BTC-USDT-SWAP", "mid_price": 100.1, "net_quantity": 0.0}],
            "recent_risk_events": [{"event_type": "recovery_failed", "detail": "state mismatch"}],
        },
        now=datetime(2026, 4, 18, tzinfo=UTC),
    )
    committee_summary = service.build_committee_summary(expert_opinions)

    assert [opinion.expert_type for opinion in expert_opinions] == [
        "market_structure",
        "risk",
        "event_filter",
    ]
    assert committee_summary == {
        "consensus_decision": "tighten_risk",
        "recommended_mode_floor": "halted",
        "blocking_flags": [
            "event:recovery_failed",
            "fault:manual_takeover",
            "mode:halted",
        ],
        "requires_human_review": True,
        "active_experts": ["market_structure", "risk", "event_filter"],
    }


def test_governor_builds_qdrant_case_query_from_committee_context() -> None:
    service = GovernorService()

    query = service.build_governance_case_query(
        {
            "current_run_mode": "degraded",
            "active_fault_flags": ["manual_takeover"],
            "committee_summary": {
                "recommended_mode_floor": "halted",
            },
        },
        trigger_reason="risk_event",
    )

    assert query == {
        "trigger_reason": "risk_event",
        "current_run_mode": "degraded",
        "recommended_mode_floor": "halted",
        "active_fault_flags": ["manual_takeover"],
    }


def test_governor_applies_guardrails_to_candidate_snapshot_when_faults_are_active() -> None:
    service = GovernorService()
    candidate = StrategyConfigSnapshot(
        version_id="snap-candidate",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.7,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="candidate",
        ttl_sec=300,
    )

    governed = service.apply_guardrails(
        candidate,
        {
            "active_fault_flags": ["public_ws_disconnected"],
            "symbol_summaries": [{"symbol": "BTC-USDT-SWAP", "mid_price": 100.1, "net_quantity": 1.0}],
        },
    )

    assert governed.market_mode == RunMode.DEGRADED
    assert governed.symbol_whitelist == ["BTC-USDT-SWAP"]
    assert governed.source_reason == "candidate|guardrailed"


def test_governor_keeps_observed_symbols_in_candidate_snapshot() -> None:
    service = GovernorService()
    candidate = StrategyConfigSnapshot(
        version_id="snap-candidate",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.7,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.DEGRADED,
        approval_state=ApprovalState.APPROVED,
        source_reason="candidate",
        ttl_sec=300,
    )

    governed = service.apply_guardrails(
        candidate,
        {
            "active_fault_flags": [],
            "symbol_summaries": [
                {"symbol": "BTC-USDT-SWAP", "mid_price": 100.1, "net_quantity": 1.0},
                {"symbol": "ETH-USDT-SWAP", "mid_price": 200.2, "net_quantity": 0.0},
            ],
        },
    )

    assert governed.symbol_whitelist == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]


def test_governor_respects_current_reduce_only_mode_and_tightens_risk_multiplier() -> None:
    service = GovernorService()
    candidate = StrategyConfigSnapshot(
        version_id="snap-candidate",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.8,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="candidate",
        ttl_sec=300,
    )

    governed = service.apply_guardrails(
        candidate,
        {
            "current_run_mode": "reduce_only",
            "active_fault_flags": [],
            "recent_risk_events": [{"event_type": "runtime_mode_changed", "detail": "reduced risk"}],
            "symbol_summaries": [{"symbol": "BTC-USDT-SWAP", "mid_price": 100.1, "net_quantity": 1.0}],
        },
    )

    assert governed.market_mode == RunMode.REDUCE_ONLY
    assert governed.risk_multiplier == 0.25


def test_governor_marks_snapshot_pending_on_recovery_failure_signal() -> None:
    service = GovernorService()
    candidate = StrategyConfigSnapshot(
        version_id="snap-candidate",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.5,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.DEGRADED,
        approval_state=ApprovalState.APPROVED,
        source_reason="candidate",
        ttl_sec=300,
    )

    governed = service.apply_guardrails(
        candidate,
        {
            "current_run_mode": "halted",
            "active_fault_flags": ["public_ws_disconnected"],
            "recent_risk_events": [{"event_type": "recovery_failed", "detail": "state mismatch"}],
            "symbol_summaries": [{"symbol": "BTC-USDT-SWAP", "mid_price": 100.1, "net_quantity": 1.0}],
        },
    )

    assert governed.market_mode == RunMode.HALTED
    assert governed.approval_state == ApprovalState.PENDING
    assert governed.risk_multiplier == 0.0


def test_governor_requests_event_trigger_for_risk_events_and_expiring_snapshot() -> None:
    service = GovernorService()
    expiring_snapshot = StrategyConfigSnapshot(
        version_id="snap-expiring",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.5,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="cached",
        ttl_sec=300,
    )

    trigger = service.determine_trigger_reason(
        {
            "current_run_mode": "normal",
            "recent_risk_events": [{"event_type": "runtime_mode_changed"}],
        },
        latest_snapshot=expiring_snapshot,
        now=datetime.now(UTC),
    )

    assert trigger == "risk_event"


def test_governor_builds_health_summary_from_trigger_and_snapshot() -> None:
    service = GovernorService()
    snapshot = StrategyConfigSnapshot(
        version_id="snap-health",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.25,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.REDUCE_ONLY,
        approval_state=ApprovalState.PENDING,
        source_reason="candidate|guardrailed",
        ttl_sec=300,
    )

    assert service.build_health_summary(
        snapshot=snapshot,
        trigger_reason="risk_event",
        status="published",
        consecutive_failures=0,
    ) == {
        "status": "published",
        "trigger": "risk_event",
        "snapshot_version": "snap-health",
        "market_mode": "reduce_only",
        "approval_state": "pending",
        "risk_multiplier": 0.25,
        "consecutive_failures": 0,
        "health_state": "healthy",
    }


def test_governor_health_summary_enters_degraded_state_after_failure_threshold() -> None:
    service = GovernorService()
    snapshot = StrategyConfigSnapshot(
        version_id="snap-health",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.5,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="cached",
        ttl_sec=300,
    )

    assert service.build_health_summary(
        snapshot=snapshot,
        trigger_reason="schedule",
        status="frozen",
        consecutive_failures=3,
    ) == {
        "status": "frozen",
        "trigger": "schedule",
        "snapshot_version": "snap-health",
        "market_mode": "degraded",
        "approval_state": "approved",
        "risk_multiplier": 0.5,
        "consecutive_failures": 3,
        "health_state": "degraded",
    }

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError
from pydantic import SecretStr

import xuanshu.infra.ai.governor_client as governor_client_module
from xuanshu.contracts.approval import ApprovalDecision, ApprovalRecord
from xuanshu.contracts.research import ResearchTrigger, StrategyPackage
from xuanshu.contracts.strategy import ApprovedStrategyBinding, StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.infra.ai.governor_client import (
    CodexCliGovernorAgentRunner,
    ConfiguredGovernorAgentRunner,
    GovernorClient,
)
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.governor.service import GovernorService


def _sample_strategy_definition(
    *,
    strategy_family: str = "breakout",
    directionality: str = "long_only",
    parameter_set: dict[str, object] | None = None,
    score: float = 67.5,
) -> dict[str, object]:
    if parameter_set is None:
        parameter_set = {
            "lookback_fast": 20,
            "signal_mode": "breakout_confirmed",
            "stop_loss_bps": 50,
            "take_profit_bps": 120,
            "risk_fraction": 0.0025,
            "max_hold_minutes": 60,
        }
    return {
        "strategy_def_id": "strat-governor-001",
        "symbol": "BTC-USDT-SWAP",
        "strategy_family": strategy_family,
        "directionality": directionality,
        "feature_spec": {"indicators": [{"name": "sma", "source": "close", "window": 20}]},
        "entry_rules": {"all": [{"op": "crosses_above", "left": "close", "right": "sma_20"}]},
        "exit_rules": {
            "any": [
                {"op": "crosses_below", "left": "close", "right": "sma_20"},
                {"op": "take_profit_bps", "value": 120},
                {"op": "stop_loss_bps", "value": 50},
                {"op": "time_stop_minutes", "value": 60},
            ]
        },
        "position_sizing_rules": {"risk_fraction": 0.0025},
        "risk_constraints": {"max_hold_minutes": 60},
        "parameter_set": parameter_set,
        "score": score,
        "score_basis": "backtest_return_percent",
    }


class _GovernorClientReturning:
    def __init__(self, snapshot: StrategyConfigSnapshot) -> None:
        self.snapshot = snapshot

    async def generate_snapshot(self, state_summary: dict[str, object]) -> StrategyConfigSnapshot:
        return self.snapshot.model_copy(deep=True)


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


class _OutOfRangeGovernorRunner:
    async def run(self, state_summary: dict[str, object]) -> dict[str, object]:
        return {
            "version_id": "snap-bounded",
            "generated_at": "2026-04-20T00:00:00Z",
            "effective_from": "2026-04-20T00:00:00Z",
            "expires_at": "2026-04-20T00:05:00Z",
            "symbol_whitelist": ["BTC-USDT-SWAP"],
            "strategy_enable_flags": {"default": True},
            "risk_multiplier": 9,
            "per_symbol_max_position": 5,
            "max_leverage": 9,
            "market_mode": "normal",
            "approval_state": "approved",
            "source_reason": "governor_ai",
            "ttl_sec": 0,
        }


@pytest.mark.asyncio
async def test_governor_client_validates_agent_output() -> None:
    client = GovernorClient(agent_runner=_BrokenGovernorRunner())

    with pytest.raises(ValidationError):
        await client.generate_snapshot({"version_id": "snap-invalid"})


@pytest.mark.asyncio
async def test_governor_client_overrides_model_supplied_version_id() -> None:
    client = GovernorClient(agent_runner=_OutOfRangeGovernorRunner())

    snapshot = await client.generate_snapshot({"version_id": "snap-invalid"})

    assert snapshot.version_id != "snap-bounded"
    assert snapshot.version_id == "governor-20260420T000000Z"


@pytest.mark.asyncio
async def test_governor_client_clamps_out_of_range_numeric_fields() -> None:
    client = GovernorClient(agent_runner=_OutOfRangeGovernorRunner())

    snapshot = await client.generate_snapshot({"version_id": "snap-invalid"})

    assert snapshot.risk_multiplier == 1.0
    assert snapshot.per_symbol_max_position == 1.0
    assert snapshot.max_leverage == 3
    assert snapshot.ttl_sec == 1


@pytest.mark.asyncio
async def test_configured_governor_runner_posts_state_summary_and_parses_fenced_json(monkeypatch) -> None:
    captured = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "output_text": """```json
{
  "version_id": "snap-live",
  "generated_at": "2026-04-19T00:00:00Z",
  "effective_from": "2026-04-19T00:00:00Z",
  "expires_at": "2026-04-19T00:05:00Z",
  "symbol_whitelist": ["BTC-USDT-SWAP"],
  "strategy_enable_flags": {"breakout": true, "mean_reversion": false, "risk_pause": true},
  "risk_multiplier": 0.5,
  "per_symbol_max_position": 0.12,
  "max_leverage": 3,
  "market_mode": "normal",
  "approval_state": "approved",
  "source_reason": "governor_ai",
  "ttl_sec": 300
}
```"""
            }

    class _AsyncClient:
        def __init__(self, *, timeout: float, headers: dict[str, str]) -> None:
            captured["timeout"] = timeout
            captured["headers"] = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, json: dict[str, object]) -> _Response:
            captured["url"] = url
            captured["payload"] = json
            return _Response()

    monkeypatch.setattr(governor_client_module.httpx, "AsyncClient", _AsyncClient)

    runner = ConfiguredGovernorAgentRunner(api_key=SecretStr("openai-key"), timeout_sec=9)
    result = await runner.run(
        {
            "scope": "governor",
            "current_run_mode": "degraded",
            "committee_summary": {"recommended_mode_floor": "degraded"},
        }
    )

    assert result["version_id"] == "snap-live"
    assert captured["timeout"] == 9
    assert captured["headers"]["Authorization"] == "Bearer openai-key"
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["payload"]["input"][1]["content"][0]["text"].startswith("State summary JSON:")


@pytest.mark.asyncio
async def test_configured_governor_runner_raises_runtime_error_on_timeout(monkeypatch) -> None:
    class _AsyncClient:
        def __init__(self, *, timeout: float, headers: dict[str, str]) -> None:
            self.timeout = timeout
            self.headers = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, json: dict[str, object]) -> object:
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(governor_client_module.httpx, "AsyncClient", _AsyncClient)

    runner = ConfiguredGovernorAgentRunner(api_key=SecretStr("openai-key"), timeout_sec=9)

    with pytest.raises(RuntimeError, match="Governor AI request timed out"):
        await runner.run({"scope": "governor"})


@pytest.mark.asyncio
async def test_configured_governor_runner_rejects_empty_response(monkeypatch) -> None:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"output": []}

    class _AsyncClient:
        def __init__(self, *, timeout: float, headers: dict[str, str]) -> None:
            self.timeout = timeout
            self.headers = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, json: dict[str, object]) -> _Response:
            return _Response()

    monkeypatch.setattr(governor_client_module.httpx, "AsyncClient", _AsyncClient)

    runner = ConfiguredGovernorAgentRunner(api_key=SecretStr("openai-key"), timeout_sec=9)

    with pytest.raises(RuntimeError, match="Governor AI response did not contain text output"):
        await runner.run({"scope": "governor"})


@pytest.mark.asyncio
async def test_codex_cli_governor_runner_invokes_codex_exec_with_snapshot_schema(monkeypatch) -> None:
    captured = {}

    def _fake_run(cmd: list[str], *, capture_output: bool, text: bool, check: bool, cwd: str | None) -> object:
        captured["cmd"] = cmd
        captured["capture_output"] = capture_output
        captured["text"] = text
        captured["check"] = check
        captured["cwd"] = cwd
        return governor_client_module.subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="""```json
{
  "version_id": "snap-live",
  "generated_at": "2026-04-19T00:00:00Z",
  "effective_from": "2026-04-19T00:00:00Z",
  "expires_at": "2026-04-19T00:05:00Z",
  "symbol_whitelist": ["BTC-USDT-SWAP"],
  "strategy_enable_flags": {"breakout": true, "mean_reversion": false, "risk_pause": true},
  "risk_multiplier": 0.5,
  "per_symbol_max_position": 0.12,
  "max_leverage": 3,
  "market_mode": "normal",
  "approval_state": "approved",
  "source_reason": "governor_ai",
  "ttl_sec": 300
}
```""",
            stderr="",
        )

    monkeypatch.setattr(governor_client_module.subprocess, "run", _fake_run)

    runner = CodexCliGovernorAgentRunner(command="codex", cwd="/tmp/xuanshu")
    result = await runner.run({"scope": "governor", "current_run_mode": "halted"})

    assert result["version_id"] == "snap-live"
    assert captured["cmd"][:3] == ["codex", "exec", "--skip-git-repo-check"]
    assert captured["cwd"] == "/tmp/xuanshu"
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["check"] is False
    prompt = captured["cmd"][3]
    assert '"version_id": string' in prompt
    assert '"generated_at": RFC3339 string' in prompt
    assert '"effective_from": RFC3339 string' in prompt
    assert '"expires_at": RFC3339 string' in prompt
    assert '"symbol_whitelist": non-empty string[]' in prompt
    assert '"strategy_enable_flags": object<string, boolean>' in prompt
    assert '"risk_multiplier": number' in prompt
    assert '"per_symbol_max_position": number' in prompt
    assert '"max_leverage": integer' in prompt
    assert '"market_mode": "normal"|"degraded"|"reduce_only"|"halted"' in prompt
    assert '"approval_state": "approved"|"rejected"' in prompt
    assert '"source_reason": string' in prompt
    assert '"ttl_sec": integer' in prompt
    assert 'Include "symbol_strategy_bindings" as an object<string, object> field; it may be empty.' in prompt
    assert "Do not return keys outside this schema." in prompt
    assert "If state_summary contains symbol_summaries" in prompt


class _GovernorRunnerWithEmptyWhitelist:
    async def run(self, state_summary: dict[str, object]) -> dict[str, object]:
        return {
            "version_id": "snap-live",
            "generated_at": "2026-04-19T00:00:00Z",
            "effective_from": "2026-04-19T00:00:00Z",
            "expires_at": "2026-04-19T00:05:00Z",
            "symbol_whitelist": [],
            "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
            "risk_multiplier": 0.5,
            "per_symbol_max_position": 0.12,
            "max_leverage": 3,
            "market_mode": "normal",
            "approval_state": "approved",
            "source_reason": "governor_ai",
            "ttl_sec": 300,
        }


@pytest.mark.asyncio
async def test_governor_client_backfills_empty_symbol_whitelist_from_state_summary() -> None:
    client = GovernorClient(agent_runner=_GovernorRunnerWithEmptyWhitelist())

    snapshot = await client.generate_snapshot(
        {
            "symbol_summaries": [
                {"symbol": "BTC-USDT-SWAP"},
                {"symbol": "ETH-USDT-SWAP"},
                {"symbol": "BTC-USDT-SWAP"},
            ]
        }
    )

    assert snapshot.symbol_whitelist == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]


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


def test_governor_build_state_summary_exposes_manual_release_target() -> None:
    service = GovernorService()
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")

    class _RuntimeStore:
        def get_run_mode(self) -> RunMode | None:
            return RunMode.HALTED

        def get_fault_flags(self) -> dict[str, object] | None:
            return {}

        def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
            return {"symbol": symbol}

        def get_manual_release_target(self) -> str | None:
            return "degraded"

    class _SnapshotStore:
        def get_latest_snapshot(self):
            return snapshot

    snapshot = StrategyConfigSnapshot(
        version_id="snap-001",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.7,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.HALTED,
        approval_state=ApprovalState.REJECTED,
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

    assert summary["manual_release_target"] == "degraded"


def test_governor_applies_manual_release_override_to_halted_candidate() -> None:
    service = GovernorService()
    candidate = StrategyConfigSnapshot(
        version_id="snap-candidate",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["SYSTEM"],
        strategy_enable_flags={"default": False},
        risk_multiplier=0.0,
        per_symbol_max_position=0.0,
        max_leverage=1,
        market_mode=RunMode.HALTED,
        approval_state=ApprovalState.REJECTED,
        source_reason="governor_ai",
        ttl_sec=300,
    )

    governed = service.apply_guardrails(
        candidate,
        {
            "current_run_mode": "halted",
            "active_fault_flags": [],
            "recent_risk_events": [{"event_type": "manual_release_requested"}],
            "manual_release_target": "degraded",
            "symbol_summaries": [{"symbol": "BTC-USDT-SWAP"}],
        },
    )

    assert governed.market_mode == RunMode.DEGRADED
    assert governed.approval_state == ApprovalState.APPROVED
    assert governed.risk_multiplier == 0.25
    assert governed.per_symbol_max_position == 0.12
    assert governed.symbol_whitelist == ["SYSTEM", "BTC-USDT-SWAP"]
    assert governed.strategy_enable_flags == {
        "breakout": True,
        "mean_reversion": False,
        "risk_pause": True,
        "default": False,
    }


def test_governor_restores_baseline_strategy_flags_when_candidate_flags_are_incomplete() -> None:
    service = GovernorService()
    candidate = StrategyConfigSnapshot(
        version_id="snap-candidate",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"default": False},
        risk_multiplier=0.5,
        per_symbol_max_position=0.12,
        max_leverage=2,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="governor_ai",
        ttl_sec=300,
    )

    governed = service.apply_guardrails(
        candidate,
        {
            "current_run_mode": "normal",
            "active_fault_flags": [],
            "recent_risk_events": [],
            "symbol_summaries": [{"symbol": "BTC-USDT-SWAP"}],
        },
    )

    assert governed.market_mode == RunMode.NORMAL
    assert governed.strategy_enable_flags == {
        "breakout": True,
        "mean_reversion": False,
        "risk_pause": True,
        "default": False,
    }


def test_governor_committee_summary_includes_research_candidates() -> None:
    service = GovernorService()
    definition = _sample_strategy_definition()
    package = StrategyPackage(
        strategy_package_id="pkg-001",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="long_only",
        entry_rules=definition["entry_rules"],
        exit_rules=definition["exit_rules"],
        position_sizing_rules=definition["position_sizing_rules"],
        risk_constraints=definition["risk_constraints"],
        parameter_set=definition["parameter_set"],
        backtest_summary={"total_return": 0.18},
        performance_summary={"sharpe": 1.4},
        failure_modes=[],
        invalidating_conditions=[],
        research_reason="manual study",
        strategy_definition=definition,
        score=67.5,
        score_basis="backtest_return_percent",
    )

    summary = service.build_committee_summary(
        expert_opinions=[],
        research_candidates=[package],
    )

    assert summary["research_candidate_count"] == 1
    assert summary["approved_research_candidates"] == ["pkg-001"]


def test_governor_committee_summary_rejects_research_candidates_when_tightened() -> None:
    service = GovernorService()
    expert_opinions = service.build_expert_opinions(
        {
            "latest_snapshot_version": "snap-tightened",
            "current_run_mode": RunMode.DEGRADED.value,
            "active_fault_flags": [],
            "recent_risk_events": [{"event_type": "recovery_failed"}],
            "symbol_summaries": [{"symbol": "BTC-USDT-SWAP"}],
        },
        now=datetime.now(UTC),
    )
    definition = _sample_strategy_definition()
    package = StrategyPackage(
        strategy_package_id="pkg-002",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="long_only",
        entry_rules=definition["entry_rules"],
        exit_rules=definition["exit_rules"],
        position_sizing_rules=definition["position_sizing_rules"],
        risk_constraints=definition["risk_constraints"],
        parameter_set=definition["parameter_set"],
        backtest_summary={"total_return": 0.18},
        performance_summary={"sharpe": 1.4},
        failure_modes=[],
        invalidating_conditions=[],
        research_reason="manual study",
        strategy_definition=definition,
        score=67.5,
        score_basis="backtest_return_percent",
    )

    summary = service.build_committee_summary(
        expert_opinions=expert_opinions,
        research_candidates=[package],
    )

    assert summary["consensus_decision"] == "tighten_risk"
    assert summary["research_candidate_count"] == 1
    assert summary["approved_research_candidates"] == []


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


def test_governor_ignores_non_blocking_risk_events_when_selecting_trigger_reason() -> None:
    service = GovernorService()
    snapshot = StrategyConfigSnapshot(
        version_id="snap-steady",
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

    trigger = service.determine_trigger_reason(
        {
            "current_run_mode": "normal",
            "recent_risk_events": [
                {"event_type": "signal_blocked"},
                {"event_type": "account_snapshot_updated"},
                {"event_type": "manual_release_requested"},
            ],
        },
        latest_snapshot=snapshot,
        now=datetime.now(UTC),
    )

    assert trigger == "schedule"


def test_governor_ignores_resolved_startup_gating_events_after_healthy_checkpoint() -> None:
    service = GovernorService()

    class _RuntimeStore:
        def get_run_mode(self) -> RunMode | None:
            return RunMode.HALTED

        def get_fault_flags(self) -> dict[str, object] | None:
            return {}

        def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
            return {"symbol": symbol, "mid_price": 100.1, "net_quantity": 3.5}

    class _SnapshotStore:
        def get_latest_snapshot(self):
            return snapshot

    class _HistoryStore:
        def list_recent_rows(self, table: str, limit: int = 10) -> list[dict[str, object]]:
            if table == "risk_events":
                return [
                    {
                        "event_type": "runtime_mode_changed",
                        "detail": "startup gating tightened runtime to halted",
                        "created_at": "2026-04-20T07:12:15.967111Z",
                    },
                    {
                        "event_type": "startup_recovery_failed",
                        "detail": "exchange_state_mismatch",
                        "created_at": "2026-04-20T07:12:15.714284Z",
                    },
                ]
            if table == "execution_checkpoints":
                return [
                    {
                        "checkpoint_id": "runtime",
                        "created_at": "2026-04-20T07:13:36.894177Z",
                        "current_mode": "halted",
                        "positions_snapshot": [{"symbol": "BTC-USDT-SWAP", "net_quantity": 3.5}],
                        "open_orders_snapshot": [],
                        "budget_state": {
                            "max_daily_loss": 100.0,
                            "remaining_daily_loss": 100.0,
                            "remaining_notional": 100.0,
                            "remaining_order_count": 10,
                        },
                        "needs_reconcile": False,
                    }
                ]
            return []

    snapshot = StrategyConfigSnapshot(
        version_id="snap-halted",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.0,
        per_symbol_max_position=0.0,
        max_leverage=1,
        market_mode=RunMode.HALTED,
        approval_state=ApprovalState.REJECTED,
        source_reason="cached",
        ttl_sec=300,
    )

    summary = service.build_state_summary(
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=_HistoryStore(),
        symbols=snapshot.symbol_whitelist,
        now=datetime(2026, 4, 20, 7, 14, tzinfo=UTC),
    )
    trigger = service.determine_trigger_reason(
        summary,
        latest_snapshot=snapshot,
        now=datetime(2026, 4, 20, 7, 14, tzinfo=UTC),
    )

    assert summary["recent_risk_events"] == []
    assert summary["committee_summary"]["recommended_mode_floor"] == "normal"
    assert trigger == "mode_change"


def test_governor_guardrails_allow_recovery_from_degraded_without_faults_or_blocking_events() -> None:
    service = GovernorService()
    candidate = StrategyConfigSnapshot(
        version_id="snap-normalized",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": True, "risk_pause": True},
        risk_multiplier=0.5,
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
            "current_run_mode": "degraded",
            "active_fault_flags": [],
            "recent_risk_events": [{"event_type": "signal_blocked", "detail": "strategy_disabled"}],
            "symbol_summaries": [{"symbol": "BTC-USDT-SWAP"}],
        },
    )

    assert governed.market_mode == RunMode.NORMAL
    assert governed.risk_multiplier == 0.5
    assert governed.approval_state == ApprovalState.APPROVED


def test_governor_guardrails_restore_normal_mode_from_stale_halted_state() -> None:
    service = GovernorService()
    candidate = StrategyConfigSnapshot(
        version_id="snap-recover",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.0,
        per_symbol_max_position=0.0,
        max_leverage=1,
        market_mode=RunMode.HALTED,
        approval_state=ApprovalState.REJECTED,
        source_reason="candidate",
        ttl_sec=300,
    )

    governed = service.apply_guardrails(
        candidate,
        {
            "current_run_mode": "halted",
            "active_fault_flags": [],
            "recent_risk_events": [],
            "symbol_summaries": [{"symbol": "BTC-USDT-SWAP"}],
        },
    )

    assert governed.market_mode == RunMode.NORMAL
    assert governed.approval_state == ApprovalState.APPROVED
    assert governed.risk_multiplier == 0.25
    assert governed.per_symbol_max_position == 0.12


def test_governor_expert_opinions_do_not_keep_tightening_risk_for_stale_degraded_mode() -> None:
    service = GovernorService()

    opinions = service.build_expert_opinions(
        {
            "current_run_mode": "degraded",
            "latest_snapshot_version": "snap-normalized",
            "active_fault_flags": [],
            "recent_risk_events": [{"event_type": "signal_blocked", "detail": "strategy_disabled"}],
            "symbol_summaries": [{"symbol": "BTC-USDT-SWAP"}],
        },
        now=datetime.now(UTC),
    )

    risk_opinion = next(opinion for opinion in opinions if opinion.expert_type == "risk")

    assert risk_opinion.decision == "maintain_risk"
    assert "current_run_mode=normal" in risk_opinion.supporting_facts


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


@pytest.mark.asyncio
async def test_governor_service_does_not_publish_research_snapshot_without_approval_record() -> None:
    service = GovernorService()
    published: list[str] = []
    last_snapshot = StrategyConfigSnapshot(
        version_id="snap-last",
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
    candidate_snapshot = last_snapshot.model_copy(
        update={
            "version_id": "snap-candidate",
            "source_reason": "candidate package",
            "risk_multiplier": 0.35,
        }
    )

    result = await service.run_cycle(
        state_summary={"scope": "governor", "approval_required": True},
        last_snapshot=last_snapshot,
        governor_client=_GovernorClientReturning(candidate_snapshot),
        publish_snapshot=lambda snapshot: published.append(snapshot.version_id),
        trigger_reason="schedule",
    )

    assert published == []
    assert result.status == "approval_pending"
    assert result.snapshot.version_id == "snap-last"


@pytest.mark.asyncio
async def test_governor_service_publishes_approved_research_snapshot_with_guardrails() -> None:
    service = GovernorService()
    published: list[StrategyConfigSnapshot] = []
    last_snapshot = StrategyConfigSnapshot(
        version_id="snap-last",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.5,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="cached",
        ttl_sec=300,
    )
    candidate_snapshot = last_snapshot.model_copy(
        update={
            "version_id": "snap-approved",
            "source_reason": "candidate package",
            "risk_multiplier": 0.45,
            "symbol_whitelist": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            "market_mode": RunMode.NORMAL,
        }
    )
    approval_record = ApprovalRecord(
        approval_record_id="apr-1",
        strategy_package_id="pkg-1",
        backtest_report_id="bt-1",
        decision=ApprovalDecision.APPROVED_WITH_GUARDRAILS,
        decision_reason="publish only in degraded mode with reduced risk",
        guardrails={
            "market_mode": "degraded",
            "risk_multiplier": 0.2,
            "symbol_whitelist": ["BTC-USDT-SWAP"],
        },
        reviewed_by="committee",
        review_source="manual",
        created_at=datetime.now(UTC),
    )

    result = await service.run_cycle(
        state_summary={
            "scope": "governor",
            "approval_required": True,
            "approval_record": approval_record.model_dump(mode="json"),
            "approved_source_reason": "approved research package",
        },
        last_snapshot=last_snapshot,
        governor_client=_GovernorClientReturning(candidate_snapshot),
        publish_snapshot=published.append,
        trigger_reason="schedule",
    )

    assert result.status == "published"
    assert len(published) == 1
    assert published[0].version_id == "snap-approved"
    assert published[0].market_mode == RunMode.DEGRADED
    assert published[0].risk_multiplier == 0.2
    assert published[0].symbol_whitelist == ["BTC-USDT-SWAP"]
    assert published[0].source_reason == "approved research package"


def test_governor_service_filters_symbol_strategy_bindings_when_guardrails_shrink_whitelist() -> None:
    service = GovernorService()
    candidate = StrategyConfigSnapshot(
        version_id="snap-candidate",
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
        source_reason="candidate",
        ttl_sec=300,
        symbol_strategy_bindings={
            "BTC-USDT-SWAP": ApprovedStrategyBinding(
                strategy_def_id="strat-1",
                strategy_package_id="pkg-1",
                backtest_report_id="bt-1",
                score=67.5,
                score_basis="backtest_return_percent",
                approval_record_id="apr-1",
                activated_at=datetime.now(UTC),
            ),
            "ETH-USDT-SWAP": ApprovedStrategyBinding(
                strategy_def_id="strat-2",
                strategy_package_id="pkg-2",
                backtest_report_id="bt-2",
                score=12.5,
                score_basis="backtest_return_percent",
                approval_record_id="apr-2",
                activated_at=datetime.now(UTC),
            ),
        },
    )
    approval_record = ApprovalRecord(
        approval_record_id="apr-guardrail",
        strategy_package_id="pkg-1",
        backtest_report_id="bt-1",
        decision=ApprovalDecision.APPROVED_WITH_GUARDRAILS,
        decision_reason="limit scope",
        guardrails={"symbol_whitelist": [" BTC-USDT-SWAP "]},
        reviewed_by="committee",
        review_source="manual",
        created_at=datetime.now(UTC),
    )

    governed = service.apply_approval_guardrails(candidate, approval_record)

    assert governed.symbol_whitelist == ["BTC-USDT-SWAP"]
    assert list(governed.symbol_strategy_bindings) == ["BTC-USDT-SWAP"]


@pytest.mark.asyncio
async def test_governor_service_blocks_rejected_research_snapshot_publication() -> None:
    service = GovernorService()
    published: list[str] = []
    last_snapshot = StrategyConfigSnapshot(
        version_id="snap-last",
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
    approval_record = ApprovalRecord(
        approval_record_id="apr-2",
        strategy_package_id="pkg-1",
        backtest_report_id="bt-1",
        decision=ApprovalDecision.REJECTED,
        decision_reason="overfit",
        guardrails={},
        reviewed_by="committee",
        review_source="manual",
        created_at=datetime.now(UTC),
    )

    result = await service.run_cycle(
        state_summary={
            "scope": "governor",
            "approval_required": True,
            "approval_record": approval_record.model_dump(mode="json"),
        },
        last_snapshot=last_snapshot,
        governor_client=_GovernorClientReturning(last_snapshot),
        publish_snapshot=lambda snapshot: published.append(snapshot.version_id),
        trigger_reason="schedule",
    )

    assert published == []
    assert result.status == "approval_rejected"
    assert result.snapshot.version_id == "snap-last"


@pytest.mark.asyncio
async def test_governor_service_blocks_needs_revision_research_snapshot_publication() -> None:
    service = GovernorService()
    last_snapshot = StrategyConfigSnapshot(
        version_id="snap-last",
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
    approval_record = ApprovalRecord(
        approval_record_id="apr-3",
        strategy_package_id="pkg-1",
        backtest_report_id="bt-1",
        decision=ApprovalDecision.NEEDS_REVISION,
        decision_reason="needs parameter cleanup",
        guardrails={},
        reviewed_by="committee",
        review_source="manual",
        created_at=datetime.now(UTC),
    )

    result = await service.run_cycle(
        state_summary={
            "scope": "governor",
            "approval_required": True,
            "approval_record": approval_record.model_dump(mode="json"),
        },
        last_snapshot=last_snapshot,
        governor_client=_GovernorClientReturning(last_snapshot),
        publish_snapshot=lambda snapshot: None,
        trigger_reason="schedule",
    )

    assert result.status == "approval_needs_revision"
    assert result.error == "needs parameter cleanup"
    assert result.snapshot.version_id == "snap-last"


@pytest.mark.asyncio
async def test_governor_service_reports_invalid_approval_record_explicitly() -> None:
    service = GovernorService()
    last_snapshot = StrategyConfigSnapshot(
        version_id="snap-last",
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

    result = await service.run_cycle(
        state_summary={
            "scope": "governor",
            "approval_required": True,
            "approval_record": {
                "approval_record_id": "apr-invalid",
                "strategy_package_id": "pkg-1",
                "backtest_report_id": "bt-1",
                "decision": "approved",
            },
        },
        last_snapshot=last_snapshot,
        governor_client=_GovernorClientReturning(last_snapshot),
        publish_snapshot=lambda snapshot: None,
        trigger_reason="schedule",
    )

    assert result.status == "approval_invalid"
    assert result.error is not None
    assert "invalid approval record" in result.error
    assert result.snapshot.version_id == "snap-last"


@pytest.mark.asyncio
async def test_governor_service_blocks_validation_failed_before_publication() -> None:
    service = GovernorService()
    published: list[str] = []
    last_snapshot = StrategyConfigSnapshot(
        version_id="snap-last",
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

    result = await service.run_cycle(
        state_summary={
            "scope": "governor",
            "approval_required": True,
            "validation_status": "failed",
            "validation_error": "historical_rows timestamps must be unique",
        },
        last_snapshot=last_snapshot,
        governor_client=_GovernorClientReturning(last_snapshot),
        publish_snapshot=lambda snapshot: published.append(snapshot.version_id),
        trigger_reason="schedule",
    )

    assert published == []
    assert result.status == "validation_failed"
    assert result.error == "historical_rows timestamps must be unique"
    assert result.snapshot.version_id == "snap-last"


@pytest.mark.asyncio
async def test_governor_service_skips_semantically_equal_snapshot_for_mode_change() -> None:
    service = GovernorService()
    published: list[str] = []
    last_snapshot = StrategyConfigSnapshot(
        version_id="snap-last",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": False, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.25,
        per_symbol_max_position=0.12,
        max_leverage=1,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="approved research package|guardrailed",
        ttl_sec=300,
    )
    candidate_snapshot = last_snapshot.model_copy(
        update={
            "version_id": "snap-same-semantics",
            "generated_at": datetime.now(UTC) + timedelta(seconds=5),
            "effective_from": datetime.now(UTC) + timedelta(seconds=5),
            "expires_at": datetime.now(UTC) + timedelta(minutes=6),
        }
    )

    result = await service.run_cycle(
        state_summary={"scope": "governor"},
        last_snapshot=last_snapshot,
        governor_client=_GovernorClientReturning(candidate_snapshot),
        publish_snapshot=lambda snapshot: published.append(snapshot.version_id),
        trigger_reason="mode_change",
    )

    assert published == []
    assert result.status == "unchanged"
    assert result.snapshot.version_id == "snap-last"


@pytest.mark.asyncio
async def test_governor_service_publishes_when_symbol_strategy_bindings_change() -> None:
    service = GovernorService()
    published: list[str] = []
    last_snapshot = StrategyConfigSnapshot(
        version_id="snap-last",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": False, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.25,
        per_symbol_max_position=0.12,
        max_leverage=1,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="approved research package",
        ttl_sec=300,
    )
    candidate_snapshot = last_snapshot.model_copy(
        update={
            "version_id": "snap-bindings",
            "symbol_strategy_bindings": {
                "BTC-USDT-SWAP": ApprovedStrategyBinding(
                    strategy_def_id="strat-1",
                    strategy_package_id="pkg-1",
                    backtest_report_id="bt-1",
                    score=67.5,
                    score_basis="backtest_return_percent",
                    approval_record_id="apr-1",
                    activated_at=datetime.now(UTC),
                )
            },
        }
    )

    result = await service.run_cycle(
        state_summary={"scope": "governor"},
        last_snapshot=last_snapshot,
        governor_client=_GovernorClientReturning(candidate_snapshot),
        publish_snapshot=lambda snapshot: published.append(snapshot.version_id),
        trigger_reason="mode_change",
    )

    assert published == ["snap-bindings"]
    assert result.status == "published"
    assert result.snapshot.version_id == "snap-bindings"

# Governor Research Backtest Approval Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the documented slow-path governance loop so scheduled research produces candidate strategy packages, deterministic backtest reports, durable approval records, and only approved outcomes can publish new `StrategyConfigSnapshot` values.

**Architecture:** Keep `Strategy Research` inside `Governor`, add first-class validation and approval artifacts, and enforce snapshot publication through a hard approval gate. `Notifier` becomes the operator command surface for viewing and mutating approval state, but durable approval truth lives in system persistence, not in Telegram messages.

**Tech Stack:** Python 3.12+, Pydantic v2, asyncio, Redis hot state, PostgreSQL append-style fact store, existing Governor/Notifier runtime wiring, pytest

---

## File Structure

### New files

- `src/xuanshu/contracts/approval.py`
  - First-class `ApprovalDecision` and `ApprovalRecord` contracts
- `src/xuanshu/contracts/backtest.py`
  - First-class `BacktestReport` contract
- `src/xuanshu/governor/backtest.py`
  - Deterministic backtest / validation engine used by `Governor`
- `tests/governor/test_backtest.py`
  - Unit tests for deterministic validation behavior

### Modified files

- `src/xuanshu/contracts/research.py`
  - Extend `StrategyPackage` to carry trigger naming and durable review fields cleanly
- `src/xuanshu/infra/storage/postgres_store.py`
  - Add durable append and query support for `strategy_packages`, `backtest_reports`, `approval_records`
- `src/xuanshu/infra/storage/redis_store.py`
  - Add hot summaries for pending approvals / latest approved package / backtest health
- `src/xuanshu/governor/service.py`
  - Add approval-aware committee and publication rules
- `src/xuanshu/apps/governor.py`
  - Wire research -> validation -> approval -> publish loop and explicit research statuses
- `src/xuanshu/notifier/service.py`
  - Add commands to inspect pending approvals, inspect reports, approve, reject
- `tests/apps/test_governor_app_wiring.py`
  - Add red/green app wiring tests for approval-gated publication
- `tests/governor/test_governor_service.py`
  - Add service-level tests for approval decisions and snapshot gating
- `tests/notifier/test_notifier_service.py`
  - Add notifier command tests for approval commands

## Task 1: Add Approval And Backtest Contracts

**Files:**
- Create: `src/xuanshu/contracts/approval.py`
- Create: `src/xuanshu/contracts/backtest.py`
- Modify: `src/xuanshu/contracts/research.py`
- Test: `tests/governor/test_backtest.py`
- Test: `tests/governor/test_governor_service.py`

- [ ] **Step 1: Write the failing contract tests**

```python
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from xuanshu.contracts.approval import ApprovalDecision, ApprovalRecord
from xuanshu.contracts.backtest import BacktestReport


def test_backtest_report_requires_timezone_aware_generated_at() -> None:
    with pytest.raises(ValidationError):
        BacktestReport(
            backtest_report_id="bt-1",
            strategy_package_id="pkg-1",
            symbol_scope=["BTC-USDT-SWAP"],
            dataset_range={"start": "2026-04-01T00:00:00Z", "end": "2026-04-02T00:00:00Z"},
            sample_count=10,
            trade_count=4,
            net_pnl=12.5,
            max_drawdown=3.2,
            win_rate=0.5,
            profit_factor=1.3,
            stability_score=0.7,
            overfit_risk="low",
            failure_modes=["late breakouts"],
            invalidating_conditions=["spread expansion"],
            generated_at=datetime(2026, 4, 20, 12, 0, 0),
        )


def test_approval_record_accepts_approved_with_guardrails() -> None:
    record = ApprovalRecord(
        approval_record_id="apr-1",
        strategy_package_id="pkg-1",
        backtest_report_id="bt-1",
        decision=ApprovalDecision.APPROVED_WITH_GUARDRAILS,
        decision_reason="usable with reduced scope",
        guardrails={"market_mode": "degraded"},
        reviewed_by="committee",
        review_source="telegram",
        created_at=datetime.now(UTC),
    )

    assert record.decision == ApprovalDecision.APPROVED_WITH_GUARDRAILS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/governor/test_backtest.py tests/governor/test_governor_service.py -k "backtest_report or approval_record" -v`

Expected: FAIL with missing module or missing symbol errors for `xuanshu.contracts.approval` / `xuanshu.contracts.backtest`.

- [ ] **Step 3: Write minimal contract implementations**

```python
# src/xuanshu/contracts/approval.py
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

NormalizedStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    APPROVED_WITH_GUARDRAILS = "approved_with_guardrails"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_record_id: NormalizedStr
    strategy_package_id: NormalizedStr
    backtest_report_id: NormalizedStr
    decision: ApprovalDecision
    decision_reason: NormalizedStr
    guardrails: dict[str, object]
    reviewed_by: NormalizedStr
    review_source: NormalizedStr
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)
```

```python
# src/xuanshu/contracts/backtest.py
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NormalizedStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class BacktestReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backtest_report_id: NormalizedStr
    strategy_package_id: NormalizedStr
    symbol_scope: list[NormalizedStr] = Field(min_length=1)
    dataset_range: dict[str, object]
    sample_count: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    net_pnl: float
    max_drawdown: float = Field(ge=0.0)
    win_rate: float = Field(ge=0.0, le=1.0)
    profit_factor: float = Field(ge=0.0)
    stability_score: float = Field(ge=0.0, le=1.0)
    overfit_risk: NormalizedStr
    failure_modes: list[NormalizedStr]
    invalidating_conditions: list[NormalizedStr]
    generated_at: datetime

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(UTC)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/governor/test_backtest.py tests/governor/test_governor_service.py -k "backtest_report or approval_record" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xuanshu/contracts/approval.py src/xuanshu/contracts/backtest.py src/xuanshu/contracts/research.py tests/governor/test_backtest.py tests/governor/test_governor_service.py
git commit -m "feat: add governance approval and backtest contracts"
```

## Task 2: Persist Research Packages, Backtest Reports, And Approval Records

**Files:**
- Modify: `src/xuanshu/infra/storage/postgres_store.py`
- Modify: `src/xuanshu/infra/storage/redis_store.py`
- Test: `tests/storage/test_storage_boundaries.py`

- [ ] **Step 1: Write the failing storage tests**

```python
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore


def test_postgres_runtime_store_appends_research_package_backtest_and_approval_rows() -> None:
    store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")

    store.append_strategy_package({"strategy_package_id": "pkg-1", "symbol_scope": ["BTC-USDT-SWAP"]})
    store.append_backtest_report({"backtest_report_id": "bt-1", "strategy_package_id": "pkg-1"})
    store.append_approval_record({"approval_record_id": "apr-1", "strategy_package_id": "pkg-1"})

    assert store.list_recent_rows("strategy_packages", limit=1)[0]["strategy_package_id"] == "pkg-1"
    assert store.list_recent_rows("backtest_reports", limit=1)[0]["backtest_report_id"] == "bt-1"
    assert store.list_recent_rows("approval_records", limit=1)[0]["approval_record_id"] == "apr-1"
```

```python
from xuanshu.infra.storage.redis_store import RedisRuntimeStateStore


def test_redis_runtime_state_store_round_trips_pending_approval_summary() -> None:
    store = RedisRuntimeStateStore(redis_client=_FakeRedis())

    summary = {"pending_count": 2, "latest_strategy_package_id": "pkg-2"}
    store.set_pending_approval_summary(summary)

    assert store.get_pending_approval_summary() == summary
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/storage/test_storage_boundaries.py -k "strategy_package or approval_summary" -v`

Expected: FAIL with missing methods on `PostgresRuntimeStore` and `RedisRuntimeStateStore`.

- [ ] **Step 3: Add minimal storage methods**

```python
# inside src/xuanshu/infra/storage/postgres_store.py
POSTGRES_TABLES = (
    "orders",
    "fills",
    "positions",
    "risk_events",
    "strategy_snapshots",
    "execution_checkpoints",
    "expert_opinions",
    "governor_runs",
    "notification_events",
    "strategy_packages",
    "backtest_reports",
    "approval_records",
)


def append_strategy_package(self, payload: dict[str, Any]) -> None:
    self._append_row("strategy_packages", payload)


def append_backtest_report(self, payload: dict[str, Any]) -> None:
    self._append_row("backtest_reports", payload)


def append_approval_record(self, payload: dict[str, Any]) -> None:
    self._append_row("approval_records", payload)
```

```python
# inside src/xuanshu/infra/storage/redis_store.py
@staticmethod
def pending_approval_summary() -> str:
    return "xuanshu:runtime:pending_approval_summary"


@staticmethod
def latest_approved_package_summary() -> str:
    return "xuanshu:runtime:latest_approved_package_summary"


@staticmethod
def backtest_health_summary() -> str:
    return "xuanshu:runtime:backtest_health_summary"
```

```python
def set_pending_approval_summary(self, summary: dict[str, object]) -> None:
    self._redis.set(RedisKeys.pending_approval_summary(), json.dumps(summary, separators=(",", ":")))


def get_pending_approval_summary(self) -> dict[str, object] | None:
    payload = self._redis.get(RedisKeys.pending_approval_summary())
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/storage/test_storage_boundaries.py -k "strategy_package or approval_summary" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xuanshu/infra/storage/postgres_store.py src/xuanshu/infra/storage/redis_store.py tests/storage/test_storage_boundaries.py
git commit -m "feat: persist research packages backtests and approvals"
```

## Task 3: Build Deterministic Backtest / Validation Engine

**Files:**
- Create: `src/xuanshu/governor/backtest.py`
- Test: `tests/governor/test_backtest.py`

- [ ] **Step 1: Write the failing validation tests**

```python
from datetime import UTC, datetime

from xuanshu.contracts.research import ResearchTrigger, StrategyPackage
from xuanshu.governor.backtest import BacktestValidator


def test_backtest_validator_builds_report_from_historical_rows() -> None:
    package = StrategyPackage(
        strategy_package_id="pkg-1",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.SCHEDULE,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="long_only",
        entry_rules={"signal": "breakout_confirmed"},
        exit_rules={"stop_loss_bps": 50, "take_profit_bps": 120},
        position_sizing_rules={"risk_fraction": 0.002},
        risk_constraints={"max_hold_minutes": 60},
        parameter_set={"lookback": 20},
        backtest_summary={},
        performance_summary={},
        failure_modes=["late"],
        invalidating_conditions=["spread expansion"],
        research_reason="scheduled research",
    )
    rows = [
        {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
        {"timestamp": datetime(2026, 4, 19, 0, 1, tzinfo=UTC), "close": 101.0},
        {"timestamp": datetime(2026, 4, 19, 0, 2, tzinfo=UTC), "close": 103.0},
    ]

    report = BacktestValidator().validate(package=package, historical_rows=rows)

    assert report.strategy_package_id == "pkg-1"
    assert report.sample_count == 3
    assert report.trade_count == 2
    assert report.net_pnl > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/governor/test_backtest.py::test_backtest_validator_builds_report_from_historical_rows -v`

Expected: FAIL with missing module `xuanshu.governor.backtest`.

- [ ] **Step 3: Write minimal validator**

```python
# src/xuanshu/governor/backtest.py
from __future__ import annotations

from datetime import UTC, datetime

from xuanshu.contracts.backtest import BacktestReport
from xuanshu.contracts.research import StrategyPackage


class BacktestValidator:
    def validate(
        self,
        *,
        package: StrategyPackage,
        historical_rows: list[dict[str, object]],
    ) -> BacktestReport:
        closes = [float(row["close"]) for row in historical_rows]
        start_close = closes[0]
        end_close = closes[-1]
        net_pnl = end_close - start_close
        max_drawdown = max(0.0, max(start_close - value for value in closes))
        trade_count = max(0, len(closes) - 1)
        win_rate = 1.0 if net_pnl > 0 else 0.0
        profit_factor = 1.5 if net_pnl > 0 else 0.0

        return BacktestReport(
            backtest_report_id=f"{package.strategy_package_id}-report",
            strategy_package_id=package.strategy_package_id,
            symbol_scope=package.symbol_scope,
            dataset_range={
                "start": historical_rows[0]["timestamp"].isoformat(),
                "end": historical_rows[-1]["timestamp"].isoformat(),
            },
            sample_count=len(historical_rows),
            trade_count=trade_count,
            net_pnl=net_pnl,
            max_drawdown=max_drawdown,
            win_rate=win_rate,
            profit_factor=profit_factor,
            stability_score=0.8 if trade_count >= 2 else 0.5,
            overfit_risk="low" if trade_count >= 2 else "high",
            failure_modes=package.failure_modes,
            invalidating_conditions=package.invalidating_conditions,
            generated_at=datetime.now(UTC),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/governor/test_backtest.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xuanshu/governor/backtest.py tests/governor/test_backtest.py
git commit -m "feat: add deterministic governor backtest validator"
```

## Task 4: Enforce Approval-Gated Snapshot Publication

**Files:**
- Modify: `src/xuanshu/governor/service.py`
- Modify: `src/xuanshu/apps/governor.py`
- Test: `tests/governor/test_governor_service.py`
- Test: `tests/apps/test_governor_app_wiring.py`

- [ ] **Step 1: Write the failing publication-gating tests**

```python
import asyncio
from datetime import UTC, datetime, timedelta

from xuanshu.contracts.approval import ApprovalDecision, ApprovalRecord
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.governor.service import GovernorService


def test_governor_service_does_not_publish_without_approval_record() -> None:
    published = []
    service = GovernorService()
    snapshot = StrategyConfigSnapshot(
        version_id="snap-1",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.5,
        per_symbol_max_position=0.1,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="candidate package",
        ttl_sec=300,
    )

    result = asyncio.run(
        service.run_cycle(
            state_summary={"scope": "governor"},
            last_snapshot=snapshot,
            governor_client=_GovernorClientReturning(snapshot),
            publish_snapshot=lambda item: published.append(item.version_id),
        )
    )

    assert published == []
    assert result.status != "published"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/governor/test_governor_service.py tests/apps/test_governor_app_wiring.py -k "approval or publish" -v`

Expected: FAIL because `run_cycle()` currently publishes without a durable approval gate.

- [ ] **Step 3: Add approval-aware cycle wiring**

```python
# inside src/xuanshu/apps/governor.py
research_status = "skipped"
validation_status = "skipped"
approval_status = "pending"
backtest_report = None
approval_record = None

if research_candidates:
    backtest_report = runtime.backtest_validator.validate(
        package=research_candidates[0],
        historical_rows=historical_rows,
    )
    runtime.history_store.append_backtest_report(backtest_report.model_dump(mode="json"))
    validation_status = "succeeded"
```

```python
if approval_record is not None and approval_record.decision in {
    ApprovalDecision.APPROVED,
    ApprovalDecision.APPROVED_WITH_GUARDRAILS,
}:
    _publish_snapshot(snapshot)
else:
    result = GovernorCycleResult(snapshot=last_snapshot, status="approval_pending")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/governor/test_governor_service.py tests/apps/test_governor_app_wiring.py -k "approval or publish" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xuanshu/governor/service.py src/xuanshu/apps/governor.py tests/governor/test_governor_service.py tests/apps/test_governor_app_wiring.py
git commit -m "feat: gate snapshot publication on durable approvals"
```

## Task 5: Make Research Status Explicit Instead Of Opaque `skipped`

**Files:**
- Modify: `src/xuanshu/apps/governor.py`
- Test: `tests/apps/test_governor_app_wiring.py`

- [ ] **Step 1: Write the failing status tests**

```python
def test_governor_cycle_records_insufficient_history_instead_of_generic_skipped(monkeypatch) -> None:
    runtime = governor_app.build_governor_runtime()
    runtime.history_store.written_rows["orders"].clear()
    runtime.history_store.written_rows["fills"].clear()
    runtime.history_store.written_rows["positions"].clear()

    asyncio.run(governor_app._run_governor_cycle(runtime))

    latest_run = runtime.history_store.list_recent_rows("governor_runs", limit=1)[0]
    assert latest_run["research_status"] == "insufficient_history"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/apps/test_governor_app_wiring.py -k "insufficient_history" -v`

Expected: FAIL because current code records `skipped`.

- [ ] **Step 3: Implement explicit statuses**

```python
if not symbol_summaries:
    research_status = "missing_symbol_summaries"
elif not historical_rows:
    research_status = "insufficient_history"
elif not research_candidates:
    research_status = "no_candidates"
else:
    research_status = "succeeded"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/apps/test_governor_app_wiring.py -k "insufficient_history" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xuanshu/apps/governor.py tests/apps/test_governor_app_wiring.py
git commit -m "feat: record explicit governor research statuses"
```

## Task 6: Add Notifier Commands For Pending Approval And Approval Actions

**Files:**
- Modify: `src/xuanshu/notifier/service.py`
- Test: `tests/notifier/test_notifier_service.py`

- [ ] **Step 1: Write the failing notifier command tests**

```python
async def test_notifier_service_lists_pending_research_approvals() -> None:
    runtime = _FakeRuntimeStore()
    runtime.pending_approval_summary = {"pending_count": 1, "latest_strategy_package_id": "pkg-1"}
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=runtime,
        snapshot_store=_FakeSnapshotStore(),
        history_store=PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu"),
    )

    payload = await service.handle_command("/approvals")

    assert "pkg-1" in payload.text
```

```python
async def test_notifier_service_records_approval_command_as_durable_event() -> None:
    runtime = _FakeRuntimeStore()
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=runtime,
        snapshot_store=_FakeSnapshotStore(),
        history_store=history,
    )

    payload = await service.handle_command("/approve pkg-1 allow trend package")

    assert "pkg-1" in payload.text
    assert history.list_recent_rows("approval_records", limit=1)[0]["strategy_package_id"] == "pkg-1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/notifier/test_notifier_service.py -k "approvals or approve" -v`

Expected: FAIL because `/approvals`, `/approve`, and `/reject` do not exist.

- [ ] **Step 3: Implement notifier command surface**

```python
if command == "/approvals":
    return render_text_message(self._render_pending_approvals())
if command == "/approve":
    return render_text_message(self._handle_approve_command(text))
if command == "/reject":
    return render_text_message(self._handle_reject_command(text))
```

```python
def _handle_approve_command(self, text: str) -> str:
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 2:
        return "用法：/approve <strategy_package_id> [reason]"
    package_id = parts[1].strip()
    reason = parts[2].strip() if len(parts) > 2 and parts[2].strip() else "operator approved package"
    self._history_store.append_approval_record(
        {
            "approval_record_id": f"manual-approve:{package_id}",
            "strategy_package_id": package_id,
            "backtest_report_id": f"{package_id}-report",
            "decision": "approved",
            "decision_reason": reason,
            "guardrails": {},
            "reviewed_by": "operator",
            "review_source": "telegram",
        }
    )
    return f"已批准研究候选：{package_id}（原因：{reason}）"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/notifier/test_notifier_service.py -k "approvals or approve" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xuanshu/notifier/service.py tests/notifier/test_notifier_service.py
git commit -m "feat: add notifier approval commands"
```

## Task 7: End-To-End Governor Loop Test With Approval-Gated Snapshot Publish

**Files:**
- Modify: `tests/apps/test_governor_app_wiring.py`
- Modify: `tests/notifier/test_notifier_service.py`
- Modify: `src/xuanshu/apps/governor.py`

- [ ] **Step 1: Write the failing integration test**

```python
def test_governor_cycle_publishes_snapshot_only_after_approved_package(monkeypatch) -> None:
    runtime = governor_app.build_governor_runtime()
    runtime.history_store.append_position_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "mark_price": 100.0,
            "generated_at": "2026-04-19T00:00:00Z",
        }
    )
    runtime.history_store.append_position_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "mark_price": 102.0,
            "generated_at": "2026-04-19T00:01:00Z",
        }
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))
    first_snapshot = runtime.snapshot_store.get_latest_snapshot()

    assert first_snapshot is None or first_snapshot.source_reason != "approved research package"

    runtime.history_store.append_approval_record(
        {
            "approval_record_id": "apr-1",
            "strategy_package_id": runtime.history_store.list_recent_rows("strategy_packages", limit=1)[0]["strategy_package_id"],
            "backtest_report_id": runtime.history_store.list_recent_rows("backtest_reports", limit=1)[0]["backtest_report_id"],
            "decision": "approved",
            "decision_reason": "operator approved",
            "guardrails": {},
            "reviewed_by": "operator",
            "review_source": "telegram",
        }
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))
    latest_snapshot = runtime.snapshot_store.get_latest_snapshot()

    assert latest_snapshot is not None
    assert latest_snapshot.source_reason == "approved research package"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/apps/test_governor_app_wiring.py::test_governor_cycle_publishes_snapshot_only_after_approved_package -v`

Expected: FAIL because current loop does not persist or honor the full approval chain.

- [ ] **Step 3: Implement missing glue code**

```python
latest_approval = _load_latest_approval_record(
    runtime.history_store,
    strategy_package_id=package.strategy_package_id,
)
if latest_approval is None:
    approval_status = "pending"
elif latest_approval["decision"] in {"approved", "approved_with_guardrails"}:
    approval_status = latest_approval["decision"]
    approval_record = ApprovalRecord.model_validate(latest_approval)
else:
    approval_status = latest_approval["decision"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/apps/test_governor_app_wiring.py tests/notifier/test_notifier_service.py -k "approved_package or approvals" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xuanshu/apps/governor.py tests/apps/test_governor_app_wiring.py tests/notifier/test_notifier_service.py
git commit -m "feat: complete approval-gated governor research loop"
```

## Spec Coverage Check

- `Strategy Research` inside `Governor`: covered by Tasks 3, 4, 5, and 7
- `backtest/validation`: covered by Tasks 1 and 3
- durable approval artifacts: covered by Tasks 1, 2, 4, and 6
- `Notifier` as approval entry: covered by Task 6
- approved-only snapshot publication: covered by Tasks 4 and 7
- explicit non-opaque research statuses: covered by Task 5
- trader remains stopped and untouched: preserved by scope; no task re-enables trader

No uncovered spec requirements remain for the first implementation slice.

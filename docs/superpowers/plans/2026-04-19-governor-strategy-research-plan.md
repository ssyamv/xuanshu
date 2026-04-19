# Governor Strategy Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `Strategy Research` as a formal internal Governor capability that can study historical real data, backtest parameterized strategy packages, submit candidates to the committee, and publish only approved results as executable snapshots.

**Architecture:** Keep `Strategy Research` inside `Governor`, not as a new service. Add research contracts, a research engine, a deterministic backtest runner, committee integration, and a publication bridge that converts approved strategy packages into `StrategyConfigSnapshot` updates while preserving the existing `Trader` boundary.

**Tech Stack:** Python 3.12, `pydantic`, `pytest`, existing Governor/Trader contracts, Redis/PostgreSQL/Qdrant adapters

---

### Task 1: Define Research Contracts

**Files:**
- Create: `src/xuanshu/contracts/research.py`
- Modify: `tests/contracts/test_contracts.py`

- [ ] **Step 1: Write the failing contract test**

Add a test that proves the new research objects are typed and strict:

```python
from datetime import UTC, datetime

from xuanshu.contracts.research import StrategyPackage, ResearchTrigger


def test_strategy_package_contract_is_typed() -> None:
    package = StrategyPackage(
        strategy_package_id="pkg-001",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="long_short",
        entry_rules={"signal": "breakout_confirmed"},
        exit_rules={"stop_loss_bps": 50, "take_profit_bps": 120},
        position_sizing_rules={"risk_fraction": 0.0025},
        risk_constraints={"max_hold_minutes": 60},
        parameter_set={"lookback_fast": 20, "lookback_slow": 60},
        backtest_summary={"total_return": 0.18},
        performance_summary={"sharpe": 1.4},
        failure_modes=["range_whipsaw"],
        invalidating_conditions=["liquidity_collapse"],
        research_reason="manual research run",
    )

    assert package.directionality == "long_short"
```
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/contracts/test_contracts.py -q
```

Expected: FAIL with `ModuleNotFoundError` or missing symbol errors for `xuanshu.contracts.research`.

- [ ] **Step 3: Write minimal implementation**

Create `src/xuanshu/contracts/research.py` with:

```python
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class ResearchTrigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"
    EVENT = "event"


class StrategyPackage(BaseModel):
    strategy_package_id: str = Field(min_length=1)
    generated_at: datetime
    trigger: ResearchTrigger
    symbol_scope: list[str] = Field(min_length=1)
    market_environment_scope: list[str] = Field(min_length=1)
    strategy_family: str = Field(min_length=1)
    directionality: str = Field(min_length=1)
    entry_rules: dict[str, object]
    exit_rules: dict[str, object]
    position_sizing_rules: dict[str, object]
    risk_constraints: dict[str, object]
    parameter_set: dict[str, object]
    backtest_summary: dict[str, object]
    performance_summary: dict[str, object]
    failure_modes: list[str]
    invalidating_conditions: list[str]
    research_reason: str = Field(min_length=1)

    @field_validator("symbol_scope", "market_environment_scope", mode="after")
    @classmethod
    def reject_blank_entries(cls, values: list[str]) -> list[str]:
        if any(not item.strip() for item in values):
            raise ValueError("blank entries are not allowed")
        return values
```
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
uv run pytest tests/contracts/test_contracts.py -q
```

Expected: PASS with the new research contract coverage included.

- [ ] **Step 5: Commit**

```bash
git add src/xuanshu/contracts/research.py tests/contracts/test_contracts.py
git commit -m "feat: add research contracts"
```

### Task 2: Add Deterministic Backtest Engine

**Files:**
- Create: `src/xuanshu/governor/research.py`
- Create: `tests/governor/test_research.py`

- [ ] **Step 1: Write the failing research engine test**

Create a test that proves a historical dataset can be turned into a `StrategyPackage` candidate with deterministic summaries:

```python
from datetime import UTC, datetime

from xuanshu.contracts.research import ResearchTrigger
from xuanshu.governor.research import StrategyResearchEngine


def test_strategy_research_engine_builds_candidate_package() -> None:
    engine = StrategyResearchEngine()
    package = engine.build_candidate_package(
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime.now(UTC), "close": 100.0},
            {"timestamp": datetime.now(UTC), "close": 103.0},
        ],
        research_reason="manual trend study",
    )

    assert package.strategy_family == "breakout"
    assert package.symbol_scope == ["BTC-USDT-SWAP"]
    assert package.market_environment_scope == ["trend"]
```
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/governor/test_research.py -q
```

Expected: FAIL because `StrategyResearchEngine` does not exist yet.

- [ ] **Step 3: Write minimal implementation**

Create `src/xuanshu/governor/research.py` with a deterministic engine:

```python
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from xuanshu.contracts.research import ResearchTrigger, StrategyPackage


class StrategyResearchEngine:
    def build_candidate_package(
        self,
        *,
        trigger: ResearchTrigger,
        symbol_scope: list[str],
        market_environment: str,
        historical_rows: list[dict[str, object]],
        research_reason: str,
    ) -> StrategyPackage:
        return StrategyPackage(
            strategy_package_id=str(uuid4()),
            generated_at=datetime.now(UTC),
            trigger=trigger,
            symbol_scope=symbol_scope,
            market_environment_scope=[market_environment],
            strategy_family="breakout" if market_environment == "trend" else "mean_reversion",
            directionality="long_short",
            entry_rules={"source": "historical_research"},
            exit_rules={"stop_loss_bps": 50, "take_profit_bps": 120},
            position_sizing_rules={"risk_fraction": 0.0025},
            risk_constraints={"max_hold_minutes": 60},
            parameter_set={"sample_count": len(historical_rows)},
            backtest_summary={"row_count": len(historical_rows)},
            performance_summary={"score": float(len(historical_rows))},
            failure_modes=[],
            invalidating_conditions=[],
            research_reason=research_reason,
        )
```
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
uv run pytest tests/governor/test_research.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xuanshu/governor/research.py tests/governor/test_research.py
git commit -m "feat: add deterministic strategy research engine"
```

### Task 3: Integrate Research Into Governor Decision Flow

**Files:**
- Modify: `src/xuanshu/governor/service.py`
- Modify: `tests/governor/test_governor_service.py`

- [ ] **Step 1: Write the failing governor integration test**

Add a test proving committee inputs can include research candidates:

```python
from xuanshu.contracts.research import ResearchTrigger, StrategyPackage
from xuanshu.governor.service import GovernorService


def test_governor_committee_summary_includes_research_candidates() -> None:
    service = GovernorService()
    package = StrategyPackage(
        strategy_package_id="pkg-001",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="long_short",
        entry_rules={"signal": "breakout_confirmed"},
        exit_rules={"stop_loss_bps": 50},
        position_sizing_rules={"risk_fraction": 0.0025},
        risk_constraints={"max_hold_minutes": 60},
        parameter_set={"lookback_fast": 20},
        backtest_summary={"total_return": 0.18},
        performance_summary={"sharpe": 1.4},
        failure_modes=[],
        invalidating_conditions=[],
        research_reason="manual study",
    )

    summary = service.build_committee_summary(
        expert_opinions=[],
        research_candidates=[package],
    )

    assert summary["research_candidate_count"] == 1
```
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/governor/test_governor_service.py -q
```

Expected: FAIL because `build_committee_summary` does not accept or report research candidates yet.

- [ ] **Step 3: Write minimal implementation**

Update `src/xuanshu/governor/service.py` so committee summary accepts `research_candidates` and returns:

```python
{
    "research_candidate_count": len(research_candidates),
    "approved_research_candidates": [
        candidate.strategy_package_id for candidate in research_candidates
    ],
    ...
}
```

Preserve existing opinion-based behavior; do not change unrelated committee rules.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
uv run pytest tests/governor/test_governor_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xuanshu/governor/service.py tests/governor/test_governor_service.py
git commit -m "feat: wire research candidates into committee summary"
```

### Task 4: Publish Approved Research As Snapshot Inputs

**Files:**
- Modify: `src/xuanshu/apps/governor.py`
- Modify: `tests/apps/test_governor_app_wiring.py`

- [ ] **Step 1: Write the failing app wiring test**

Add a test that proves approved research output is translated into a published snapshot field update:

```python
def test_governor_cycle_can_publish_snapshot_from_approved_research(monkeypatch) -> None:
    ...
    assert runtime.last_snapshot.source_reason == "approved research package"
```
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/apps/test_governor_app_wiring.py -q
```

Expected: FAIL because governor runtime does not yet translate approved research packages into snapshot publication inputs.

- [ ] **Step 3: Write minimal implementation**

Update `src/xuanshu/apps/governor.py` to:

- construct a `StrategyResearchEngine`
- build at least one candidate package in controlled cases
- pass the candidate into committee evaluation
- when approved, stamp the snapshot `source_reason` with an explicit research-origin reason

Keep the scope minimal: no new service process, no asynchronous job queue, no external AI orchestration rewrite.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
uv run pytest tests/apps/test_governor_app_wiring.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xuanshu/apps/governor.py tests/apps/test_governor_app_wiring.py
git commit -m "feat: publish approved research through governor runtime"
```

### Task 5: Document Triggering And Operational Use

**Files:**
- Modify: `docs/operations/runbook.md`
- Modify: `docs/operations/alerts.md`

- [ ] **Step 1: Write the failing documentation contract test**

Add a test that checks the operations docs mention:

- manual research trigger
- event-triggered research
- committee approval boundary

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/apps/test_trader_app_wiring.py -q
```

Expected: FAIL because the docs do not mention research operations yet.

- [ ] **Step 3: Write minimal implementation**

Update the runbook and alerts docs so operators know:

- research is part of Governor
- it may be schedule/manual/event triggered
- approved research is required before it can influence execution

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
uv run pytest tests/apps/test_trader_app_wiring.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/operations/runbook.md docs/operations/alerts.md tests/apps/test_trader_app_wiring.py
git commit -m "docs: add research operations guidance"
```

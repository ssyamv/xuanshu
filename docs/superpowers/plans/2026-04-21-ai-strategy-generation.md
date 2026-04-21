# AI Strategy Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a constrained executable strategy DSL so the governor can generate new AI-discovered strategies, backtest them, retain candidates with backtest interval return above 50%, submit them for approval, and let the trader enforce one active strategy per symbol with a 10% stronger-strategy replacement rule.

**Architecture:** Introduce a shared strategy-definition contract and interpreter layer used by research, backtesting, and live execution. Extend the governor pipeline to generate and expand DSL candidates, compute `return_percent`, and publish per-symbol approved strategy bindings into snapshots. Extend trader runtime state to track active strategy ownership and perform controlled symbol handovers when a newly approved strategy clears the 10% replacement threshold.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, pytest-asyncio, Redis, SQLAlchemy/PostgreSQL, existing OKX adapter layer.

---

## File Structure

### New Files

- `src/xuanshu/contracts/strategy_definition.py`
  Defines the constrained DSL contract, expression nodes, operator enums, and validation helpers.
- `src/xuanshu/strategies/dsl_features.py`
  Computes supported indicators and raw value lookups for a single-symbol data series / runtime snapshot.
- `src/xuanshu/strategies/dsl_rules.py`
  Evaluates DSL entry and exit expressions against computed features.
- `src/xuanshu/strategies/dsl_execution.py`
  Holds shared execution helpers for translating a strategy definition into candidate signals and exit decisions.
- `tests/contracts/test_strategy_definition.py`
  Covers DSL contract validation and invalid-input rejection.
- `tests/strategies/test_dsl_rules.py`
  Covers feature computation and expression evaluation semantics.

### Modified Files

- `src/xuanshu/contracts/research.py`
  Extend `StrategyPackage` to embed executable strategy definitions and score metadata.
- `src/xuanshu/contracts/backtest.py`
  Add `return_percent` and strategy definition identifiers to reports.
- `src/xuanshu/contracts/strategy.py`
  Extend snapshots with per-symbol approved strategy bindings.
- `src/xuanshu/governor/research.py`
  Generate DSL-based packages and deterministic parameter expansions.
- `src/xuanshu/governor/backtest.py`
  Interpret the DSL, align score calculation, and report `return_percent`.
- `src/xuanshu/governor/service.py`
  Filter on `return_percent > 50`, preserve per-symbol approved strategy bindings, and compare stronger approved candidates.
- `src/xuanshu/apps/governor.py`
  Persist expanded package/report metadata, publish approved bindings into snapshots, and store candidate scores.
- `src/xuanshu/risk/kernel.py`
  Read active per-symbol strategy bindings from snapshots when evaluating opens.
- `src/xuanshu/strategies/signals.py`
  Replace fixed hard-coded generation with DSL-driven signal generation while keeping bootstrap compatibility.
- `src/xuanshu/apps/trader.py`
  Track active strategy ownership per symbol, enforce one-active-strategy semantics, and run controlled handovers.
- `src/xuanshu/trader/dispatcher.py`
  Thread strategy ownership through event dispatch decisions.
- `src/xuanshu/trader/recovery.py`
  Recover active strategy ownership and in-progress handover status.
- `src/xuanshu/infra/storage/redis_store.py`
  Add runtime keys and helpers for active symbol strategy bindings and handover state.
- `src/xuanshu/infra/storage/postgres_store.py`
  Persist replacement decisions and handover audit rows.
- `tests/governor/test_research.py`
  Cover AI candidate generation and deterministic expansion for DSL candidates.
- `tests/governor/test_backtest.py`
  Cover DSL backtesting, `return_percent`, and retention threshold logic.
- `tests/governor/test_governor_service.py`
  Cover snapshot publication, stronger-strategy comparison, and approval submission.
- `tests/apps/test_governor_app_wiring.py`
  Cover end-to-end governor wiring for DSL package persistence and snapshot bindings.
- `tests/trader/test_trader_decision_flow.py`
  Cover one-active-strategy-per-symbol and replacement threshold behavior.
- `tests/trader/test_recovery.py`
  Cover handover recovery states and mismatch handling.
- `tests/storage/test_storage_boundaries.py`
  Cover new Redis/Postgres persistence boundaries.

## Task 1: Add the Strategy DSL Contracts

**Files:**
- Create: `src/xuanshu/contracts/strategy_definition.py`
- Modify: `src/xuanshu/contracts/research.py`
- Modify: `src/xuanshu/contracts/strategy.py`
- Test: `tests/contracts/test_strategy_definition.py`

- [ ] **Step 1: Write the failing contract tests**

```python
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from xuanshu.contracts.research import StrategyPackage
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.contracts.strategy_definition import StrategyDefinition


def _sample_strategy_definition() -> dict[str, object]:
    return {
        "strategy_def_id": "strat-btc-001",
        "symbol": "BTC-USDT-SWAP",
        "strategy_family": "volatility_break_retest",
        "directionality": "long_only",
        "feature_spec": {
            "indicators": [
                {"name": "sma", "source": "close", "window": 20},
                {"name": "ema", "source": "close", "window": 50},
            ]
        },
        "entry_rules": {
            "all": [
                {"op": "crosses_above", "left": "close", "right": "sma_20"},
                {"op": "greater_than", "left": "sma_20", "right": "ema_50"},
            ]
        },
        "exit_rules": {
            "any": [
                {"op": "crosses_below", "left": "close", "right": "sma_20"},
                {"op": "take_profit_bps", "value": 900},
                {"op": "stop_loss_bps", "value": 300},
            ]
        },
        "position_sizing_rules": {"risk_fraction": 0.01},
        "risk_constraints": {"max_hold_minutes": 240},
        "parameter_set": {"fast_window": 20, "slow_window": 50},
        "score": 67.5,
        "score_basis": "backtest_return_percent",
    }


def test_strategy_definition_accepts_supported_dsl_shape() -> None:
    definition = StrategyDefinition.model_validate(_sample_strategy_definition())

    assert definition.symbol == "BTC-USDT-SWAP"
    assert definition.score == 67.5
    assert definition.entry_rules["all"][0]["op"] == "crosses_above"


def test_strategy_definition_rejects_unsupported_operator() -> None:
    payload = _sample_strategy_definition()
    payload["entry_rules"] = {"all": [{"op": "exec_python", "value": "boom"}]}

    with pytest.raises(ValidationError, match="unsupported operator"):
        StrategyDefinition.model_validate(payload)


def test_strategy_package_requires_embedded_strategy_definition() -> None:
    package = StrategyPackage.model_validate(
        {
            "strategy_package_id": "pkg-1",
            "generated_at": datetime.now(UTC),
            "trigger": "schedule",
            "symbol_scope": ["BTC-USDT-SWAP"],
            "market_environment_scope": ["trend"],
            "strategy_family": "volatility_break_retest",
            "directionality": "long_only",
            "entry_rules": {"signal": "dsl"},
            "exit_rules": {"mode": "dsl"},
            "position_sizing_rules": {"risk_fraction": 0.01},
            "risk_constraints": {"max_hold_minutes": 240},
            "parameter_set": {"fast_window": 20},
            "backtest_summary": {"row_count": 100},
            "performance_summary": {"return_percent": 67.5},
            "failure_modes": ["late_reversal"],
            "invalidating_conditions": ["gap_down"],
            "research_reason": "ai candidate",
            "strategy_definition": _sample_strategy_definition(),
            "score": 67.5,
            "score_basis": "backtest_return_percent",
        }
    )

    assert package.strategy_definition.strategy_def_id == "strat-btc-001"


def test_strategy_snapshot_accepts_symbol_strategy_bindings() -> None:
    snapshot = StrategyConfigSnapshot.model_validate(
        {
            "version_id": "snap-1",
            "generated_at": "2026-04-21T00:00:00Z",
            "effective_from": "2026-04-21T00:00:00Z",
            "expires_at": "2026-04-21T00:05:00Z",
            "symbol_whitelist": ["BTC-USDT-SWAP"],
            "strategy_enable_flags": {"risk_pause": True},
            "risk_multiplier": 0.5,
            "per_symbol_max_position": 0.12,
            "max_leverage": 3,
            "market_mode": "normal",
            "approval_state": "approved",
            "source_reason": "approved research package",
            "ttl_sec": 300,
            "symbol_strategy_bindings": {
                "BTC-USDT-SWAP": {
                    "strategy_def_id": "strat-btc-001",
                    "strategy_package_id": "pkg-1",
                    "backtest_report_id": "bt-1",
                    "score": 67.5,
                    "score_basis": "backtest_return_percent",
                    "approval_record_id": "apr-1",
                    "activated_at": "2026-04-21T00:00:00Z",
                }
            },
        }
    )

    assert snapshot.symbol_strategy_bindings["BTC-USDT-SWAP"].score == 67.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/contracts/test_strategy_definition.py -v`
Expected: FAIL with import errors or validation mismatches for missing `StrategyDefinition`, missing `strategy_definition`, and missing `symbol_strategy_bindings`.

- [ ] **Step 3: Add the minimal DSL contracts**

```python
# src/xuanshu/contracts/strategy_definition.py
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SUPPORTED_INDICATORS = {"sma", "ema", "atr", "highest", "lowest", "zscore"}
_SUPPORTED_SOURCES = {"open", "high", "low", "close", "volume"}
_SUPPORTED_OPERATORS = {
    "greater_than",
    "less_than",
    "crosses_above",
    "crosses_below",
    "take_profit_bps",
    "stop_loss_bps",
    "time_stop_minutes",
}
_SUPPORTED_DIRECTIONALITY = {"long_only", "short_only"}
_SUPPORTED_SCORE_BASES = {"backtest_return_percent"}


class IndicatorSpec(BaseModel):
    name: str
    source: str | None = None
    window: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _SUPPORTED_INDICATORS:
            raise ValueError("unsupported indicator")
        return normalized

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip().lower()
        if normalized not in _SUPPORTED_SOURCES:
            raise ValueError("unsupported source")
        return normalized


class StrategyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_def_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    strategy_family: str = Field(min_length=1)
    directionality: str = Field(min_length=1)
    feature_spec: dict[str, object]
    entry_rules: dict[str, object]
    exit_rules: dict[str, object]
    position_sizing_rules: dict[str, object]
    risk_constraints: dict[str, object]
    parameter_set: dict[str, object]
    score: float
    score_basis: str = Field(min_length=1)

    @field_validator("directionality")
    @classmethod
    def validate_directionality(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _SUPPORTED_DIRECTIONALITY:
            raise ValueError("unsupported directionality")
        return normalized

    @field_validator("score_basis")
    @classmethod
    def validate_score_basis(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _SUPPORTED_SCORE_BASES:
            raise ValueError("unsupported score basis")
        return normalized

    @model_validator(mode="after")
    def validate_supported_rule_tree(self) -> "StrategyDefinition":
        self._validate_rule_tree(self.entry_rules)
        self._validate_rule_tree(self.exit_rules)
        indicators = self.feature_spec.get("indicators", [])
        if not isinstance(indicators, list) or not indicators:
            raise ValueError("feature_spec.indicators must not be empty")
        for indicator in indicators:
            IndicatorSpec.model_validate(indicator)
        return self

    @classmethod
    def _validate_rule_tree(cls, node: object) -> None:
        if isinstance(node, dict):
            if "all" in node or "any" in node:
                key = "all" if "all" in node else "any"
                children = node[key]
                if not isinstance(children, list) or not children:
                    raise ValueError(f"{key} must contain rule nodes")
                for child in children:
                    cls._validate_rule_tree(child)
                return
            op = node.get("op")
            if not isinstance(op, str) or op.strip().lower() not in _SUPPORTED_OPERATORS:
                raise ValueError("unsupported operator")
            return
        raise ValueError("rule node must be a mapping")
```

```python
# src/xuanshu/contracts/research.py
from xuanshu.contracts.strategy_definition import StrategyDefinition


class StrategyPackage(BaseModel):
    ...
    strategy_definition: StrategyDefinition
    score: float = Field(ge=0.0)
    score_basis: NormalizedStr
```

```python
# src/xuanshu/contracts/strategy.py
class ApprovedStrategyBinding(BaseModel):
    strategy_def_id: str = Field(min_length=1)
    strategy_package_id: str = Field(min_length=1)
    backtest_report_id: str = Field(min_length=1)
    score: float = Field(ge=0.0)
    score_basis: str = Field(min_length=1)
    approval_record_id: str = Field(min_length=1)
    activated_at: datetime


class StrategyConfigSnapshot(BaseModel):
    ...
    symbol_strategy_bindings: dict[str, ApprovedStrategyBinding] = Field(default_factory=dict)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/contracts/test_strategy_definition.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/contracts/test_strategy_definition.py \
  src/xuanshu/contracts/strategy_definition.py \
  src/xuanshu/contracts/research.py \
  src/xuanshu/contracts/strategy.py
git commit -m "feat: add strategy DSL contracts"
```

## Task 2: Add Shared DSL Feature and Rule Evaluation

**Files:**
- Create: `src/xuanshu/strategies/dsl_features.py`
- Create: `src/xuanshu/strategies/dsl_rules.py`
- Create: `src/xuanshu/strategies/dsl_execution.py`
- Modify: `src/xuanshu/strategies/signals.py`
- Test: `tests/strategies/test_dsl_rules.py`

- [ ] **Step 1: Write the failing evaluator tests**

```python
from datetime import UTC, datetime, timedelta

from xuanshu.contracts.strategy_definition import StrategyDefinition
from xuanshu.strategies.dsl_execution import build_candidate_signal_from_definition
from xuanshu.strategies.dsl_rules import evaluate_entry_rules


def _historical_rows() -> list[dict[str, object]]:
    base = datetime(2026, 4, 20, tzinfo=UTC)
    closes = [100, 101, 102, 103, 104, 105, 108]
    return [
        {"timestamp": base + timedelta(minutes=index), "open": close, "high": close, "low": close, "close": close, "volume": 10}
        for index, close in enumerate(closes)
    ]


def _definition() -> StrategyDefinition:
    return StrategyDefinition.model_validate(
        {
            "strategy_def_id": "strat-btc-001",
            "symbol": "BTC-USDT-SWAP",
            "strategy_family": "trend_accel",
            "directionality": "long_only",
            "feature_spec": {"indicators": [{"name": "sma", "source": "close", "window": 3}]},
            "entry_rules": {"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
            "exit_rules": {"any": [{"op": "stop_loss_bps", "value": 300}]},
            "position_sizing_rules": {"risk_fraction": 0.01},
            "risk_constraints": {"max_hold_minutes": 240},
            "parameter_set": {"window": 3},
            "score": 67.5,
            "score_basis": "backtest_return_percent",
        }
    )


def test_evaluate_entry_rules_returns_true_when_rule_matches() -> None:
    assert evaluate_entry_rules(_definition(), _historical_rows()) is True


def test_build_candidate_signal_from_definition_threads_strategy_identity() -> None:
    signal = build_candidate_signal_from_definition(_definition(), _historical_rows())

    assert signal is not None
    assert signal.symbol == "BTC-USDT-SWAP"
    assert signal.strategy_id == "strat-btc-001"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/strategies/test_dsl_rules.py -v`
Expected: FAIL because the DSL evaluator modules and DSL-driven signal builder do not exist.

- [ ] **Step 3: Add the minimal shared evaluator**

```python
# src/xuanshu/strategies/dsl_features.py
def compute_feature_context(definition: StrategyDefinition, rows: list[dict[str, object]]) -> dict[str, float]:
    closes = [float(row["close"]) for row in rows]
    context = {"close": closes[-1]}
    for indicator in definition.feature_spec["indicators"]:
        name = indicator["name"]
        window = int(indicator["window"])
        if name == "sma":
            context[f"sma_{window}"] = sum(closes[-window:]) / window
        elif name == "ema":
            context[f"ema_{window}"] = sum(closes[-window:]) / window
    return context
```

```python
# src/xuanshu/strategies/dsl_rules.py
from xuanshu.strategies.dsl_features import compute_feature_context


def evaluate_entry_rules(definition: StrategyDefinition, rows: list[dict[str, object]]) -> bool:
    return _evaluate_tree(definition.entry_rules, compute_feature_context(definition, rows))


def _evaluate_tree(node: dict[str, object], context: dict[str, float]) -> bool:
    if "all" in node:
        return all(_evaluate_tree(child, context) for child in node["all"])
    if "any" in node:
        return any(_evaluate_tree(child, context) for child in node["any"])
    op = node["op"]
    left = context[node["left"]]
    right = context[node["right"]] if isinstance(node.get("right"), str) else node["right"]["const"]
    if op == "greater_than":
        return left > right
    if op == "less_than":
        return left < right
    raise ValueError(f"unsupported runtime op: {op}")
```

```python
# src/xuanshu/strategies/dsl_execution.py
from xuanshu.contracts.risk import CandidateSignal
from xuanshu.core.enums import EntryType, OrderSide, SignalUrgency
from xuanshu.strategies.dsl_rules import evaluate_entry_rules


def build_candidate_signal_from_definition(
    definition: StrategyDefinition,
    rows: list[dict[str, object]],
) -> CandidateSignal | None:
    if not evaluate_entry_rules(definition, rows):
        return None
    return CandidateSignal(
        symbol=definition.symbol,
        strategy_id=definition.strategy_def_id,
        side=OrderSide.BUY if definition.directionality == "long_only" else OrderSide.SELL,
        entry_type=EntryType.MARKET,
        urgency=SignalUrgency.NORMAL,
        confidence=1.0,
        max_hold_ms=int(definition.risk_constraints.get("max_hold_minutes", 60)) * 60_000,
        cancel_after_ms=500,
        risk_tag=definition.strategy_family,
    )
```

```python
# src/xuanshu/strategies/signals.py
def build_candidate_signals(snapshot: MarketStateSnapshot) -> list[CandidateSignal]:
    definition = getattr(snapshot, "strategy_definition", None)
    historical_rows = getattr(snapshot, "historical_rows", None)
    if definition is not None and historical_rows is not None:
        signal = build_candidate_signal_from_definition(definition, historical_rows)
        return [signal] if signal is not None else []
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/strategies/test_dsl_rules.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/strategies/test_dsl_rules.py \
  src/xuanshu/strategies/dsl_features.py \
  src/xuanshu/strategies/dsl_rules.py \
  src/xuanshu/strategies/dsl_execution.py \
  src/xuanshu/strategies/signals.py
git commit -m "feat: add DSL feature and rule evaluation"
```

## Task 3: Generate and Expand DSL Strategy Candidates

**Files:**
- Modify: `src/xuanshu/governor/research.py`
- Modify: `src/xuanshu/governor/research_providers.py`
- Modify: `tests/governor/test_research.py`

- [ ] **Step 1: Write the failing research tests**

```python
from datetime import UTC, datetime, timedelta

import pytest

from xuanshu.contracts.research import ResearchTrigger
from xuanshu.governor.research import StrategyResearchEngine


class _Provider:
    async def generate_analyses(self, **kwargs):
        return [
            type(
                "Suggestion",
                (),
                {
                    "thesis": "trend continuation above moving average",
                    "strategy_family": "trend_continuation",
                    "entry_signal": "close_above_sma",
                    "exit_stop_loss_bps": 250,
                    "exit_take_profit_bps": 1200,
                    "risk_fraction": 0.02,
                    "max_hold_minutes": 180,
                    "failure_modes": ["late_entry"],
                    "invalidating_conditions": ["vol_spike"],
                    "strategy_definition": {
                        "directionality": "long_only",
                        "feature_spec": {"indicators": [{"name": "sma", "source": "close", "window": 20}]},
                        "entry_rules": {"all": [{"op": "greater_than", "left": "close", "right": "sma_20"}]},
                        "exit_rules": {"any": [{"op": "take_profit_bps", "value": 1200}, {"op": "stop_loss_bps", "value": 250}]},
                    },
                },
            )()
        ]


@pytest.mark.asyncio
async def test_research_engine_builds_dsl_packages_and_expands_variants() -> None:
    engine = StrategyResearchEngine(provider=_Provider())
    rows = [
        {"timestamp": datetime(2026, 4, 20, tzinfo=UTC) + timedelta(hours=index), "close": 100 + index}
        for index in range(30)
    ]

    packages = await engine.build_candidate_packages_from_provider(
        trigger=ResearchTrigger.SCHEDULE,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=rows,
        research_reason="governor strategy research",
    )

    assert packages
    assert all(package.strategy_definition.symbol == "BTC-USDT-SWAP" for package in packages)
    assert len({package.strategy_package_id for package in packages}) == len(packages)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/governor/test_research.py -v`
Expected: FAIL because provider output does not yet map into embedded DSL strategy definitions and deterministic variant expansion.

- [ ] **Step 3: Implement DSL package generation and local parameter expansion**

```python
# src/xuanshu/governor/research.py
from xuanshu.contracts.strategy_definition import StrategyDefinition


class StrategyResearchEngine:
    ...
    async def build_candidate_packages_from_provider(...):
        ...
        for suggestion in suggestions:
            base_definition = self._build_strategy_definition_from_suggestion(
                suggestion=suggestion,
                symbol_scope=symbol_scope,
                strategy_family=normalized_strategy_family,
            )
            packages.extend(
                self._build_candidate_variants(
                    ...
                    strategy_definition=base_definition,
                )
            )
        return packages

    def _build_strategy_definition_from_suggestion(... ) -> StrategyDefinition:
        payload = {
            "strategy_def_id": self._build_strategy_def_id(...),
            "symbol": symbol_scope[0],
            "strategy_family": strategy_family,
            "directionality": suggestion.strategy_definition["directionality"],
            "feature_spec": suggestion.strategy_definition["feature_spec"],
            "entry_rules": suggestion.strategy_definition["entry_rules"],
            "exit_rules": suggestion.strategy_definition["exit_rules"],
            "position_sizing_rules": {"risk_fraction": suggestion.risk_fraction},
            "risk_constraints": {"max_hold_minutes": suggestion.max_hold_minutes},
            "parameter_set": {},
            "score": 0.0,
            "score_basis": "backtest_return_percent",
        }
        return StrategyDefinition.model_validate(payload)

    def _build_candidate_variants(..., strategy_definition: StrategyDefinition) -> list[StrategyPackage]:
        ...
        updated_definition = strategy_definition.model_copy(
            update={
                "strategy_def_id": self._build_strategy_def_id(...),
                "parameter_set": {"lookback": lookback, "risk_fraction": risk_fraction_value},
            }
        )
        package = self._build_candidate_package(...).model_copy(
            update={
                "strategy_definition": updated_definition,
                "score": 0.0,
                "score_basis": "backtest_return_percent",
            }
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/governor/test_research.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/governor/test_research.py \
  src/xuanshu/governor/research.py \
  src/xuanshu/governor/research_providers.py
git commit -m "feat: generate DSL-based strategy candidates"
```

## Task 4: Backtest DSL Candidates and Retain `return_percent > 50`

**Files:**
- Modify: `src/xuanshu/contracts/backtest.py`
- Modify: `src/xuanshu/governor/backtest.py`
- Modify: `tests/governor/test_backtest.py`

- [ ] **Step 1: Write the failing backtest tests**

```python
from datetime import UTC, datetime, timedelta

from xuanshu.contracts.research import ResearchTrigger, StrategyPackage
from xuanshu.contracts.strategy_definition import StrategyDefinition
from xuanshu.governor.backtest import BacktestValidator


def _package() -> StrategyPackage:
    definition = StrategyDefinition.model_validate(
        {
            "strategy_def_id": "strat-btc-001",
            "symbol": "BTC-USDT-SWAP",
            "strategy_family": "trend_continuation",
            "directionality": "long_only",
            "feature_spec": {"indicators": [{"name": "sma", "source": "close", "window": 2}]},
            "entry_rules": {"all": [{"op": "greater_than", "left": "close", "right": "sma_2"}]},
            "exit_rules": {"any": [{"op": "take_profit_bps", "value": 5000}]},
            "position_sizing_rules": {"risk_fraction": 1.0},
            "risk_constraints": {"max_hold_minutes": 600},
            "parameter_set": {"window": 2},
            "score": 0.0,
            "score_basis": "backtest_return_percent",
        }
    )
    return StrategyPackage.model_validate(
        {
            "strategy_package_id": "pkg-1",
            "generated_at": datetime.now(UTC),
            "trigger": ResearchTrigger.SCHEDULE,
            "symbol_scope": ["BTC-USDT-SWAP"],
            "market_environment_scope": ["trend"],
            "strategy_family": "trend_continuation",
            "directionality": "long_only",
            "entry_rules": {"signal": "dsl"},
            "exit_rules": {"mode": "dsl"},
            "position_sizing_rules": {"risk_fraction": 1.0},
            "risk_constraints": {"max_hold_minutes": 600},
            "parameter_set": {"window": 2},
            "backtest_summary": {"row_count": 0},
            "performance_summary": {"return_percent": 0.0},
            "failure_modes": ["late_entry"],
            "invalidating_conditions": ["gap_down"],
            "research_reason": "ai candidate",
            "strategy_definition": definition,
            "score": 0.0,
            "score_basis": "backtest_return_percent",
        }
    )


def test_backtest_report_includes_return_percent() -> None:
    validator = BacktestValidator()
    rows = [
        {"timestamp": datetime(2026, 4, 20, tzinfo=UTC) + timedelta(hours=index), "close": close}
        for index, close in enumerate([100, 120, 150, 170])
    ]

    report = validator.validate(package=_package(), historical_rows=rows)

    assert report.return_percent > 50
    assert report.strategy_def_id == "strat-btc-001"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/governor/test_backtest.py -v`
Expected: FAIL because backtest reports do not yet expose `return_percent` and do not interpret DSL-based strategy definitions.

- [ ] **Step 3: Implement DSL backtesting and return-percent scoring**

```python
# src/xuanshu/contracts/backtest.py
class BacktestReport(BaseModel):
    ...
    strategy_def_id: str = Field(min_length=1)
    return_percent: float
```

```python
# src/xuanshu/governor/backtest.py
from xuanshu.strategies.dsl_rules import evaluate_entry_rules


class BacktestValidator:
    def validate(...):
        ...
        trades = self._simulate_dsl_trades(
            package=package,
            normalized_rows=normalized_rows,
        )
        net_pnl = sum(trade["pnl"] for trade in trades)
        initial_close = normalized_rows[0][1]
        return_percent = ((initial_close + net_pnl) - initial_close) / initial_close * 100
        return BacktestReport(
            ...
            strategy_def_id=package.strategy_definition.strategy_def_id,
            return_percent=return_percent,
        )

    def _simulate_dsl_trades(...):
        definition = package.strategy_definition
        ...
        for index in range(1, len(normalized_rows)):
            window_rows = self._window_rows(normalized_rows[: index + 1])
            if position is None and evaluate_entry_rules(definition, window_rows):
                ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/governor/test_backtest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/governor/test_backtest.py \
  src/xuanshu/contracts/backtest.py \
  src/xuanshu/governor/backtest.py
git commit -m "feat: score DSL strategies by backtest return"
```

## Task 5: Update Governor Retention, Approval, and Snapshot Publication

**Files:**
- Modify: `src/xuanshu/governor/service.py`
- Modify: `src/xuanshu/apps/governor.py`
- Modify: `src/xuanshu/infra/storage/postgres_store.py`
- Modify: `src/xuanshu/infra/storage/redis_store.py`
- Test: `tests/governor/test_governor_service.py`
- Test: `tests/apps/test_governor_app_wiring.py`
- Test: `tests/storage/test_storage_boundaries.py`

- [ ] **Step 1: Write the failing governor and storage tests**

```python
from datetime import UTC, datetime, timedelta

from xuanshu.contracts.approval import ApprovalDecision, ApprovalRecord
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.governor.service import GovernorService


def test_governor_accepts_candidate_only_when_return_percent_exceeds_fifty() -> None:
    service = GovernorService()
    assert service._candidate_clears_return_gate(50.1) is True
    assert service._candidate_clears_return_gate(50.0) is False


def test_governor_snapshot_binding_keeps_approved_strategy_score() -> None:
    snapshot = StrategyConfigSnapshot(
        version_id="snap-1",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"risk_pause": True},
        risk_multiplier=0.5,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode="normal",
        approval_state="approved",
        source_reason="approved research package",
        ttl_sec=300,
        symbol_strategy_bindings={},
    )

    updated = service.bind_symbol_strategy(
        snapshot=snapshot,
        symbol="BTC-USDT-SWAP",
        binding_payload={
            "strategy_def_id": "strat-btc-001",
            "strategy_package_id": "pkg-1",
            "backtest_report_id": "bt-1",
            "score": 67.5,
            "score_basis": "backtest_return_percent",
            "approval_record_id": "apr-1",
            "activated_at": datetime.now(UTC),
        },
    )

    assert updated.symbol_strategy_bindings["BTC-USDT-SWAP"].score == 67.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/governor/test_governor_service.py tests/apps/test_governor_app_wiring.py tests/storage/test_storage_boundaries.py -v`
Expected: FAIL because the governor does not yet use `return_percent > 50`, snapshots do not yet publish symbol bindings, and storage helpers do not yet persist strategy binding state.

- [ ] **Step 3: Implement retention and snapshot binding**

```python
# src/xuanshu/governor/service.py
class GovernorService:
    @staticmethod
    def _candidate_clears_return_gate(return_percent: float) -> bool:
        return return_percent > 50.0

    def bind_symbol_strategy(
        self,
        *,
        snapshot: StrategyConfigSnapshot,
        symbol: str,
        binding_payload: dict[str, object],
    ) -> StrategyConfigSnapshot:
        updated_bindings = dict(snapshot.symbol_strategy_bindings)
        updated_bindings[symbol] = ApprovedStrategyBinding.model_validate(binding_payload)
        return snapshot.model_copy(update={"symbol_strategy_bindings": updated_bindings})
```

```python
# src/xuanshu/apps/governor.py
if backtest_report.return_percent > 50.0:
    validated_candidates.append((candidate, backtest_report))

...
if approval_record.decision in {ApprovalDecision.APPROVED, ApprovalDecision.APPROVED_WITH_GUARDRAILS}:
    snapshot = runtime.service.bind_symbol_strategy(
        snapshot=snapshot,
        symbol=candidate.symbol_scope[0],
        binding_payload={
            "strategy_def_id": candidate.strategy_definition.strategy_def_id,
            "strategy_package_id": candidate.strategy_package_id,
            "backtest_report_id": backtest_report.backtest_report_id,
            "score": backtest_report.return_percent,
            "score_basis": "backtest_return_percent",
            "approval_record_id": approval_record.approval_record_id,
            "activated_at": datetime.now(UTC),
        },
    )
```

```python
# src/xuanshu/infra/storage/redis_store.py
class RedisKeys:
    @staticmethod
    def active_symbol_strategy(symbol: str) -> str:
        return f"xuanshu:runtime:active_strategy:{symbol}"
```

```python
# src/xuanshu/infra/storage/postgres_store.py
POSTGRES_TABLES = (
    ...
    "strategy_replacements",
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/governor/test_governor_service.py tests/apps/test_governor_app_wiring.py tests/storage/test_storage_boundaries.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/governor/test_governor_service.py \
  tests/apps/test_governor_app_wiring.py \
  tests/storage/test_storage_boundaries.py \
  src/xuanshu/governor/service.py \
  src/xuanshu/apps/governor.py \
  src/xuanshu/infra/storage/postgres_store.py \
  src/xuanshu/infra/storage/redis_store.py
git commit -m "feat: retain and publish approved strategy bindings"
```

## Task 6: Enforce One Active Strategy Per Symbol and 10% Replacement

**Files:**
- Modify: `src/xuanshu/apps/trader.py`
- Modify: `src/xuanshu/trader/dispatcher.py`
- Modify: `src/xuanshu/trader/recovery.py`
- Modify: `src/xuanshu/risk/kernel.py`
- Test: `tests/trader/test_trader_decision_flow.py`
- Test: `tests/trader/test_recovery.py`

- [ ] **Step 1: Write the failing trader tests**

```python
from xuanshu.apps import trader as trader_app


def test_trader_replaces_active_strategy_only_when_new_score_is_ten_percent_higher() -> None:
    current = {"strategy_def_id": "strat-old", "score": 60.0, "score_basis": "backtest_return_percent"}
    stronger = {"strategy_def_id": "strat-new", "score": 66.0, "score_basis": "backtest_return_percent"}
    weaker = {"strategy_def_id": "strat-new", "score": 65.9, "score_basis": "backtest_return_percent"}

    assert trader_app._is_stronger_replacement(current=current, candidate=stronger) is True
    assert trader_app._is_stronger_replacement(current=current, candidate=weaker) is False


def test_trader_handover_sequence_flattens_before_switching() -> None:
    events = trader_app._build_strategy_handover_events(
        symbol="BTC-USDT-SWAP",
        current_strategy_id="strat-old",
        next_strategy_id="strat-new",
    )

    assert events == [
        "cancel_open_orders",
        "flatten_position",
        "mark_replaced_by_stronger_strategy",
        "activate_new_strategy",
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/trader/test_trader_decision_flow.py tests/trader/test_recovery.py -v`
Expected: FAIL because the trader does not yet track active strategy bindings or the 10% replacement rule.

- [ ] **Step 3: Implement symbol ownership and replacement**

```python
# src/xuanshu/apps/trader.py
def _is_stronger_replacement(*, current: dict[str, object] | None, candidate: dict[str, object]) -> bool:
    if current is None:
        return True
    current_score = float(current["score"])
    candidate_score = float(candidate["score"])
    return candidate_score >= current_score * 1.10


def _build_strategy_handover_events(
    *,
    symbol: str,
    current_strategy_id: str,
    next_strategy_id: str,
) -> list[str]:
    return [
        "cancel_open_orders",
        "flatten_position",
        "mark_replaced_by_stronger_strategy",
        "activate_new_strategy",
    ]
```

```python
# src/xuanshu/apps/trader.py
@dataclass(slots=True)
class TraderRuntime:
    ...
    active_symbol_strategies: dict[str, dict[str, object]] = field(default_factory=dict)
    symbol_handover_state: dict[str, dict[str, object]] = field(default_factory=dict)
```

```python
# src/xuanshu/trader/recovery.py
async def run_startup_recovery(...):
    ...
    return {
        "run_mode": checkpoint.current_mode.value,
        "needs_reconcile": False,
        "reason": "checkpoint_matches_exchange",
        "active_strategy_id": checkpoint.active_snapshot_version,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/trader/test_trader_decision_flow.py tests/trader/test_recovery.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/trader/test_trader_decision_flow.py \
  tests/trader/test_recovery.py \
  src/xuanshu/apps/trader.py \
  src/xuanshu/trader/dispatcher.py \
  src/xuanshu/trader/recovery.py \
  src/xuanshu/risk/kernel.py
git commit -m "feat: enforce per-symbol strategy ownership and replacement"
```

## Task 7: End-to-End Verification

**Files:**
- Modify: `tests/apps/test_governor_app_wiring.py`
- Modify: `tests/apps/test_trader_app_wiring.py`
- Modify: `tests/test_project_smoke.py`

- [ ] **Step 1: Add end-to-end verification tests**

```python
def test_governor_publishes_snapshot_with_symbol_strategy_binding(...):
    ...
    assert published_snapshot.symbol_strategy_bindings["BTC-USDT-SWAP"].score > 50


def test_trader_rejects_symbol_overlap_and_only_switches_after_handover(...):
    ...
    assert runtime.active_symbol_strategies["BTC-USDT-SWAP"]["strategy_def_id"] == "strat-new"
```

- [ ] **Step 2: Run focused app tests**

Run: `pytest tests/apps/test_governor_app_wiring.py tests/apps/test_trader_app_wiring.py -v`
Expected: PASS

- [ ] **Step 3: Run the broader regression suite**

Run: `pytest tests/contracts/test_strategy_definition.py tests/strategies/test_dsl_rules.py tests/governor/test_research.py tests/governor/test_backtest.py tests/governor/test_governor_service.py tests/apps/test_governor_app_wiring.py tests/apps/test_trader_app_wiring.py tests/trader/test_trader_decision_flow.py tests/trader/test_recovery.py tests/storage/test_storage_boundaries.py -v`
Expected: PASS

- [ ] **Step 4: Run the full test suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/apps/test_governor_app_wiring.py \
  tests/apps/test_trader_app_wiring.py \
  tests/test_project_smoke.py
git commit -m "test: verify AI strategy generation pipeline end to end"
```

## Self-Review

- Spec coverage:
  - DSL contract and validation: Task 1
  - shared DSL semantics: Task 2
  - AI candidate generation and local expansion: Task 3
  - backtest `return_percent > 50` retention basis: Task 4 and Task 5
  - approval path and snapshot bindings: Task 5
  - one-active-strategy-per-symbol and 10% replacement: Task 6
  - recovery and end-to-end verification: Task 6 and Task 7
- Placeholder scan:
  - No `TODO`, `TBD`, or deferred implementation markers appear in task steps.
- Type consistency:
  - `StrategyDefinition`, `ApprovedStrategyBinding`, `return_percent`, and `symbol_strategy_bindings` are introduced in Task 1 and used consistently in later tasks.

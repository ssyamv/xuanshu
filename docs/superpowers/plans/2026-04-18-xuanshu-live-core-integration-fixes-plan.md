# 玄枢 Live Core Integration Fixes Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three remaining high-priority integration gaps in the live-core skeleton: trader runtime must do real work, governor must execute a minimal governance cycle and publish snapshots, and checkpoint budget state must participate in open-risk gating.

**Architecture:** Preserve the current single-host service split, but move each app from “constructed and blocked” to a minimal executable loop. `Trader` will perform startup validation, checkpoint gating, and a lightweight runtime loop around real configured dependencies; `Governor` will perform a periodic governance cycle using the existing governor client and publish/freeze snapshots; `CheckpointService` will enforce both reconcile state and exhausted budget state. Keep the scope tight: no new product features, only executable integration of what the skeleton already promised.

**Tech Stack:** Python 3.12, `asyncio`, `pydantic`, `pydantic-settings`, `httpx`, `websockets`, `redis`, `sqlalchemy`, `structlog`, `pytest`, `pytest-asyncio`, Docker Compose.

---

## Scope Check

This follow-up plan is intentionally narrow. It fixes the remaining gaps from final review and does not reopen the broader skeleton plan.

In scope:

- Trader runtime loop and dependency wiring
- Governor runtime loop and snapshot publication/freeze flow
- Checkpoint budget-state gating
- Tests proving the above behavior

Out of scope:

- Real OKX websocket consumption
- Real Telegram delivery
- Real OpenAI agent orchestration beyond the already defined client boundary
- Backtesting, MLflow, replay, or case retrieval expansion

## File Structure

Likely touch points for this cleanup batch:

- `src/xuanshu/apps/trader.py`: trader runtime settings, runtime object, startup loop
- `src/xuanshu/apps/governor.py`: governor runtime settings, periodic governance cycle
- `src/xuanshu/checkpoints/service.py`: reconcile + budget gating
- `src/xuanshu/governor/service.py`: publish/freeze behavior for snapshots
- `src/xuanshu/infra/storage/redis_store.py`: latest snapshot hot-state interaction if needed
- `src/xuanshu/infra/storage/postgres_store.py`: snapshot/checkpoint fact interaction if needed
- `src/xuanshu/infra/ai/governor_client.py`: only if the runtime loop needs a typed call surface refinement
- `tests/apps/test_trader_app_wiring.py`: trader runtime behavior
- `tests/apps/test_governor_app_wiring.py`: governor cycle behavior
- `tests/execution/test_execution_and_recovery.py`: budget gating behavior

## Task 1: Integrate Checkpoint Budget Gating

**Files:**
- Modify: `src/xuanshu/checkpoints/service.py`
- Modify: `tests/execution/test_execution_and_recovery.py`

- [ ] **Step 1: Write the failing budget-gating test**

Add to `tests/execution/test_execution_and_recovery.py`:

```python
def test_checkpoint_blocks_new_risk_when_budget_is_exhausted() -> None:
    exhausted_checkpoint = ExecutionCheckpoint(
        checkpoint_id="cp-003",
        created_at=datetime.now(UTC),
        active_snapshot_version="snap-003",
        current_mode=RunMode.NORMAL,
        positions_snapshot=[],
        open_orders_snapshot=[],
        budget_state=CheckpointBudgetState(
            max_daily_loss=100.0,
            remaining_daily_loss=0.0,
            remaining_notional=0.0,
            remaining_order_count=0,
        ),
        last_public_stream_marker="pub-3",
        last_private_stream_marker="pri-3",
        needs_reconcile=False,
    )

    assert CheckpointService().can_open_new_risk(exhausted_checkpoint) is False
```

- [ ] **Step 2: Run the execution/recovery test to verify it fails**

Run:

```bash
pytest tests/execution/test_execution_and_recovery.py -v
```

Expected: FAIL because `can_open_new_risk()` still returns `True` when budget is exhausted.

- [ ] **Step 3: Implement budget-aware gating**

Update `src/xuanshu/checkpoints/service.py`:

```python
from xuanshu.contracts.checkpoint import ExecutionCheckpoint


class CheckpointService:
    def can_open_new_risk(self, checkpoint: ExecutionCheckpoint) -> bool:
        if checkpoint.needs_reconcile:
            return False

        budget = checkpoint.budget_state
        if budget.remaining_daily_loss <= 0:
            return False
        if budget.remaining_notional <= 0:
            return False
        if budget.remaining_order_count <= 0:
            return False

        return True
```

- [ ] **Step 4: Run the execution/recovery test to verify it passes**

Run:

```bash
pytest tests/execution/test_execution_and_recovery.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/checkpoints/service.py tests/execution/test_execution_and_recovery.py
git commit -m "fix: enforce checkpoint budget gating"
```

## Task 2: Make Governor Runtime Execute A Minimal Governance Cycle

**Files:**
- Modify: `src/xuanshu/governor/service.py`
- Modify: `src/xuanshu/apps/governor.py`
- Modify: `tests/apps/test_governor_app_wiring.py`

- [ ] **Step 1: Write the failing governor-cycle test**

Replace/add in `tests/apps/test_governor_app_wiring.py`:

```python
import xuanshu.apps.governor as governor_app
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode


def test_governor_runtime_runs_one_cycle_and_keeps_snapshot(monkeypatch) -> None:
    published_versions: list[str] = []

    async def _noop_wait_forever() -> None:
        return None

    async def fake_run_cycle(runtime: governor_app.GovernorRuntime) -> None:
        snapshot = await runtime.service.run_cycle(
            state_summary={"symbol": "BTC-USDT-SWAP"},
            last_snapshot=runtime.last_snapshot,
            governor_client=runtime.governor_client,
            publish_snapshot=lambda item: published_versions.append(item.version_id),
        )
        runtime.last_snapshot = snapshot

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(governor_app, "_run_governor", fake_run_cycle)

    class _Runner:
        async def run(self, state_summary):
            return {
                "version_id": "snap-new",
                "generated_at": "2026-04-18T00:00:00Z",
                "effective_from": "2026-04-18T00:00:00Z",
                "expires_at": "2026-04-18T00:05:00Z",
                "symbol_whitelist": ["BTC-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
                "risk_multiplier": 0.5,
                "per_symbol_max_position": 0.12,
                "max_leverage": 3,
                "market_mode": RunMode.NORMAL,
                "approval_state": ApprovalState.APPROVED,
                "source_reason": "cycle",
                "ttl_sec": 300,
            }

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: governor_app.GovernorClient(_Runner()))

    assert governor_app.main() == 0
    assert published_versions == ["snap-new"]
```

- [ ] **Step 2: Run the governor app test to verify it fails**

Run:

```bash
pytest tests/apps/test_governor_app_wiring.py -v
```

Expected: FAIL because there is no real governance cycle yet.

- [ ] **Step 3: Implement a minimal governance cycle and publication flow**

Update `src/xuanshu/governor/service.py`:

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.infra.ai.governor_client import GovernorClient


class GovernorService:
    def freeze_on_failure(self, last_snapshot: StrategyConfigSnapshot) -> StrategyConfigSnapshot:
        return last_snapshot.model_copy(deep=True)

    async def run_cycle(
        self,
        state_summary: Mapping[str, object],
        last_snapshot: StrategyConfigSnapshot,
        governor_client: GovernorClient,
        publish_snapshot: Callable[[StrategyConfigSnapshot], None],
    ) -> StrategyConfigSnapshot:
        try:
            snapshot = await governor_client.generate_snapshot(state_summary)
        except Exception:
            snapshot = self.freeze_on_failure(last_snapshot)
        publish_snapshot(snapshot)
        return snapshot
```

Update `src/xuanshu/apps/governor.py` to carry a configured runtime and execute one cycle before blocking:

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from xuanshu.config.settings import GovernorRuntimeSettings
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.governor.service import GovernorService
from xuanshu.infra.ai.governor_client import ConfiguredGovernorAgentRunner, GovernorClient


@dataclass(slots=True)
class GovernorRuntime:
    settings: GovernorRuntimeSettings
    service: GovernorService
    governor_client: GovernorClient
    last_snapshot: StrategyConfigSnapshot


def build_governor_service() -> GovernorService:
    return GovernorService()


def build_governor_client(settings: GovernorRuntimeSettings) -> GovernorClient:
    return GovernorClient(
        agent_runner=ConfiguredGovernorAgentRunner(
            api_key=settings.openai_api_key,
            timeout_sec=settings.ai_timeout_sec,
        )
    )


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
        last_snapshot=_build_bootstrap_snapshot(),
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_governor(runtime: GovernorRuntime) -> None:
    runtime.last_snapshot = await runtime.service.run_cycle(
        state_summary={"scope": "governor"},
        last_snapshot=runtime.last_snapshot,
        governor_client=runtime.governor_client,
        publish_snapshot=lambda item: None,
    )
    await _wait_forever()


def main() -> int:
    asyncio.run(_run_governor(build_governor_runtime()))
    return 0
```

- [ ] **Step 4: Run the governor app test to verify it passes**

Run:

```bash
pytest tests/apps/test_governor_app_wiring.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/governor/service.py src/xuanshu/apps/governor.py tests/apps/test_governor_app_wiring.py
git commit -m "fix: run a minimal governor cycle"
```

## Task 3: Make Trader Runtime Exercise Startup State And Recent-Flow Semantics

**Files:**
- Modify: `src/xuanshu/state/engine.py`
- Modify: `src/xuanshu/apps/trader.py`
- Modify: `tests/trader/test_trader_decision_flow.py`
- Modify: `tests/apps/test_trader_app_wiring.py`
- Modify: `.env.example`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Write the failing cold-start/runtime test**

Add to `tests/trader/test_trader_decision_flow.py`:

```python
def test_trader_does_not_emit_trade_signal_without_quotes() -> None:
    engine = StateEngine()
    engine.on_trade("BTC-USDT-SWAP", price=100.0, size=1.0, side="buy")

    snapshot = engine.snapshot("BTC-USDT-SWAP")
    signals = build_candidate_signals(snapshot)

    assert snapshot.regime == MarketRegime.UNKNOWN
    assert signals[0].strategy_id == StrategyId.RISK_PAUSE
```

Add to `tests/apps/test_trader_app_wiring.py`:

```python
def test_trader_runtime_loads_starting_nav_from_settings(monkeypatch) -> None:
    monkeypatch.setenv("XUANSHU_TRADER_STARTING_NAV", "250000")
    runtime = trader_app.build_trader_runtime()
    assert runtime.starting_nav == 250000.0
```

- [ ] **Step 2: Run the trader tests to verify they fail**

Run:

```bash
pytest tests/trader/test_trader_decision_flow.py tests/apps/test_trader_app_wiring.py -v
```

Expected: FAIL because cold-start/no-BBO still emits a tradable signal or trader runtime does not expose configured NAV correctly.

- [ ] **Step 3: Implement bounded recent-flow state and startup runtime wiring**

Update `src/xuanshu/state/engine.py` so recent-flow is based on a bounded recent trade window and quote absence forces `UNKNOWN` regime:

```python
from collections import deque
from dataclasses import dataclass, field
...


@dataclass
class SymbolState:
    bid: float | None = None
    ask: float | None = None
    recent_trade_sides: deque[str] = field(default_factory=lambda: deque(maxlen=20))


@dataclass
class StateEngine:
    symbols: dict[str, SymbolState] = field(default_factory=dict)
    ...

    def on_trade(self, symbol: str, price: float, size: float, side: str) -> None:
        ...
        state.recent_trade_sides.append(normalized_side)

    def snapshot(self, symbol: str) -> MarketStateSnapshot:
        state = self.symbols[symbol]
        if state.bid is None or state.ask is None:
            return MarketStateSnapshot(
                snapshot_id=str(uuid4()),
                generated_at=datetime.now(UTC),
                symbol=symbol,
                mid_price=0.0,
                spread=0.0,
                imbalance=0.0,
                recent_trade_bias=0.0,
                volatility_state=VolatilityState.NORMAL,
                regime=MarketRegime.UNKNOWN,
                current_position=0.0,
                current_mode=RunMode.NORMAL,
                risk_budget_remaining=1.0,
            )
        ...
        buys = sum(1 for item in state.recent_trade_sides if item == "buy")
        sells = sum(1 for item in state.recent_trade_sides if item == "sell")
        total_trades = buys + sells
        if total_trades == 0:
            recent_trade_bias = 0.0
            regime = MarketRegime.UNKNOWN
        else:
            recent_trade_bias = (buys - sells) / total_trades
            regime = classify_regime(snapshot)
        ...
```

Update `src/xuanshu/apps/trader.py` to expose a configured runtime object:

```python
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from xuanshu.checkpoints.service import CheckpointService
from xuanshu.config.settings import TraderRuntimeSettings
from xuanshu.execution.engine import build_client_order_id
from xuanshu.risk.kernel import RiskKernel
from xuanshu.state.engine import StateEngine


@dataclass(frozen=True, slots=True)
class TraderComponents:
    state_engine: StateEngine
    risk_kernel: RiskKernel
    checkpoint_service: CheckpointService
    client_order_id_builder: Callable[[str, str, int], str]


@dataclass(frozen=True, slots=True)
class TraderRuntime:
    settings: TraderRuntimeSettings
    components: TraderComponents
    starting_nav: float


def build_trader_components(starting_nav: float) -> TraderComponents:
    return TraderComponents(
        state_engine=StateEngine(),
        risk_kernel=RiskKernel(nav=starting_nav),
        checkpoint_service=CheckpointService(),
        client_order_id_builder=build_client_order_id,
    )


def build_trader_runtime() -> TraderRuntime:
    settings = TraderRuntimeSettings()
    return TraderRuntime(
        settings=settings,
        components=build_trader_components(starting_nav=settings.starting_nav),
        starting_nav=settings.starting_nav,
    )
...
async def _run_trader(runtime: TraderRuntime) -> None:
    _ = runtime.components
    await _wait_forever()


def main() -> int:
    asyncio.run(_run_trader(build_trader_runtime()))
    return 0
```

Update `.env.example` and `docker-compose.yml` to publish `XUANSHU_TRADER_STARTING_NAV`.

- [ ] **Step 4: Run the trader tests to verify they pass**

Run:

```bash
pytest tests/trader/test_trader_decision_flow.py tests/apps/test_trader_app_wiring.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/state/engine.py src/xuanshu/apps/trader.py tests/trader/test_trader_decision_flow.py tests/apps/test_trader_app_wiring.py .env.example docker-compose.yml
git commit -m "fix: wire trader runtime and recent-flow state"
```

## Task 4: Validate Settings And Governance Contract Inputs More Strictly

**Files:**
- Modify: `src/xuanshu/config/settings.py`
- Modify: `src/xuanshu/contracts/strategy.py`
- Modify: `src/xuanshu/contracts/governance.py`
- Modify: `tests/contracts/test_contracts.py`
- Modify: `tests/apps/test_governor_app_wiring.py`
- Modify: `tests/apps/test_notifier_app_wiring.py`

- [ ] **Step 1: Write the failing settings/contract validation tests**

Add to `tests/contracts/test_contracts.py`:

```python
def test_blank_symbol_entries_are_rejected() -> None:
    with pytest.raises(ValidationError):
        StrategyConfigSnapshot(
            version_id="snap-004",
            generated_at=datetime.now(UTC),
            effective_from=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            symbol_whitelist=[" "],
            strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
            risk_multiplier=0.5,
            per_symbol_max_position=0.12,
            max_leverage=3,
            market_mode=RunMode.NORMAL,
            approval_state=ApprovalState.APPROVED,
            source_reason="blank symbol",
            ttl_sec=300,
        )

    with pytest.raises(ValidationError):
        ExpertOpinion(
            opinion_id="op-blank",
            expert_type="risk",
            generated_at=datetime.now(UTC),
            symbol_scope=[" "],
            decision="tighten_risk",
            confidence=0.7,
            supporting_facts=["fact"],
            risk_flags=["flag"],
            ttl_sec=60,
        )
```

Add/adjust in governor/notifier app wiring tests so missing service-specific secrets fail startup.

- [ ] **Step 2: Run the validation tests to verify they fail**

Run:

```bash
pytest tests/contracts/test_contracts.py tests/apps/test_governor_app_wiring.py tests/apps/test_notifier_app_wiring.py -v
```

Expected: FAIL because blank symbol entries are still accepted or startup does not fail on missing service-specific secrets.

- [ ] **Step 3: Implement stricter validation**

Update `src/xuanshu/contracts/strategy.py` and `src/xuanshu/contracts/governance.py` with validators that reject blank/whitespace-only symbol entries.

Update `src/xuanshu/config/settings.py` so:

- `TraderRuntimeSettings` owns trader-specific startup requirements
- `GovernorRuntimeSettings` owns OpenAI-specific requirements
- `NotifierRuntimeSettings` owns Telegram-specific requirements

Keep shared infrastructure settings available, but do not force governor/notifier startup to require unrelated Redis/Postgres/Qdrant config if those are unused at startup.

- [ ] **Step 4: Run the validation tests to verify they pass**

Run:

```bash
pytest tests/contracts/test_contracts.py tests/apps/test_governor_app_wiring.py tests/apps/test_notifier_app_wiring.py -v
```

Expected: PASS.

- [ ] **Step 5: Run the full test suite**

Run:

```bash
pytest tests -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/xuanshu/config/settings.py src/xuanshu/contracts/strategy.py src/xuanshu/contracts/governance.py tests/contracts/test_contracts.py tests/apps/test_governor_app_wiring.py tests/apps/test_notifier_app_wiring.py
git commit -m "fix: tighten runtime settings and governance contracts"
```

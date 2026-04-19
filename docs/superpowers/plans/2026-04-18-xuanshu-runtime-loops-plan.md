# 玄枢 Runtime Loops Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the current live-core skeleton into a minimal executable single-host loop: trader must actually consult checkpoint/runtime state before idling, governor must publish snapshots to a shared boundary instead of an in-memory list, and notifier must have a real outbound delivery call surface.

**Architecture:** Keep the current lightweight single-host design, but remove the “build objects then wait forever” gap. `Trader` will run a startup/runtime tick that checks checkpoint gating and computes one non-trading state pass before entering its wait loop. `Governor` will publish snapshots into the existing hot-state boundary. `Notifier` will translate a notification event into a concrete Telegram send call surface. This is still not a full production engine; it is the first minimal executable runtime path.

**Tech Stack:** Python 3.12, `asyncio`, `pydantic`, `pydantic-settings`, `httpx`, `redis`, `pytest`, `pytest-asyncio`.

---

## Scope Check

This plan is intentionally about executable runtime loops, not feature expansion.

In scope:

- Trader startup/runtime execution path
- Governor snapshot publication to shared boundary
- Notifier outbound send boundary
- Tests proving these paths execute

Out of scope:

- Full OKX websocket event loop
- Real Redis/Postgres persistence implementation
- Real Telegram bot networking
- Real OpenAI agent implementation beyond the existing client boundary
- End-to-end multi-service orchestration

## File Structure

Likely touch points:

- `src/xuanshu/apps/trader.py`
- `src/xuanshu/apps/governor.py`
- `src/xuanshu/apps/notifier.py`
- `src/xuanshu/checkpoints/service.py`
- `src/xuanshu/infra/storage/redis_store.py`
- `src/xuanshu/infra/notifier/telegram.py`
- `tests/apps/test_trader_app_wiring.py`
- `tests/apps/test_governor_app_wiring.py`
- `tests/apps/test_notifier_app_wiring.py`

## Task 1: Make Trader Runtime Execute A Real Startup Tick

**Files:**
- Modify: `src/xuanshu/apps/trader.py`
- Modify: `tests/apps/test_trader_app_wiring.py`

- [ ] **Step 1: Write the failing trader runtime test**

Add to `tests/apps/test_trader_app_wiring.py`:

```python
def test_trader_runtime_checks_checkpoint_before_waiting(monkeypatch) -> None:
    seen_can_open = []

    async def _noop_wait_forever() -> None:
        return None

    class _CheckpointProbe:
        def can_open_new_risk(self, checkpoint) -> bool:
            seen_can_open.append(checkpoint.needs_reconcile)
            return False

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)

    runtime = trader_app.build_trader_runtime()
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=_CheckpointProbe(),
        okx_rest_client=runtime.components.okx_rest_client,
        okx_public_stream=runtime.components.okx_public_stream,
        okx_private_stream=runtime.components.okx_private_stream,
        client_order_id_builder=runtime.components.client_order_id_builder,
    )

    asyncio.run(trader_app._run_trader(runtime))

    assert seen_can_open == [False]
```

- [ ] **Step 2: Run the trader app test to verify it fails**

Run:

```bash
pytest tests/apps/test_trader_app_wiring.py -v
```

Expected: FAIL because `_run_trader()` does not yet consult checkpoint state.

- [ ] **Step 3: Implement a real startup tick**

Update `src/xuanshu/apps/trader.py`:

```python
@dataclass(slots=True)
class TraderRuntime:
    settings: TraderRuntimeSettings
    components: TraderComponents
    starting_nav: float
    startup_checkpoint: ExecutionCheckpoint


def _build_startup_checkpoint() -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        checkpoint_id="startup",
        created_at=datetime.now(UTC),
        active_snapshot_version="bootstrap",
        current_mode=RunMode.NORMAL,
        positions_snapshot=[],
        open_orders_snapshot=[],
        budget_state=CheckpointBudgetState(
            max_daily_loss=100.0,
            remaining_daily_loss=100.0,
            remaining_notional=100.0,
            remaining_order_count=10,
        ),
        last_public_stream_marker=None,
        last_private_stream_marker=None,
        needs_reconcile=False,
    )


def build_trader_runtime() -> TraderRuntime:
    settings = TraderRuntimeSettings()
    return TraderRuntime(
        settings=settings,
        components=build_trader_components(settings=settings),
        starting_nav=settings.trader_starting_nav,
        startup_checkpoint=_build_startup_checkpoint(),
    )


async def _run_trader(runtime: TraderRuntime) -> None:
    runtime.components.checkpoint_service.can_open_new_risk(runtime.startup_checkpoint)
    await _wait_forever()
```

- [ ] **Step 4: Run the trader app test to verify it passes**

Run:

```bash
pytest tests/apps/test_trader_app_wiring.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/apps/trader.py tests/apps/test_trader_app_wiring.py
git commit -m "fix: run trader startup tick"
```

## Task 2: Publish Governor Snapshots To A Shared Boundary

**Files:**
- Modify: `src/xuanshu/apps/governor.py`
- Modify: `src/xuanshu/infra/storage/redis_store.py`
- Modify: `tests/apps/test_governor_app_wiring.py`

- [ ] **Step 1: Write the failing governor publication test**

Add to `tests/apps/test_governor_app_wiring.py`:

```python
def test_governor_runtime_publishes_snapshot_to_store(monkeypatch) -> None:
    published = []

    class _Store:
        def set_latest_snapshot(self, version_id: str, snapshot) -> None:
            published.append((version_id, snapshot.version_id))

    async def _noop_wait_forever() -> None:
        return None

    class _Runner:
        async def run(self, state_summary):
            return {
                "version_id": "snap-store",
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

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_snapshot_store", lambda settings: _Store())

    runtime = governor_app.build_governor_runtime()
    asyncio.run(governor_app._run_governor(runtime))

    assert published == [("snap-store", "snap-store")]
```

- [ ] **Step 2: Run the governor app test to verify it fails**

Run:

```bash
pytest tests/apps/test_governor_app_wiring.py -v
```

Expected: FAIL because runtime does not yet publish to a shared store.

- [ ] **Step 3: Implement snapshot-store publication**

Update `src/xuanshu/infra/storage/redis_store.py`:

```python
class RedisSnapshotStore:
    def __init__(self) -> None:
        self._snapshots: dict[str, object] = {}

    def set_latest_snapshot(self, version_id: str, snapshot: object) -> None:
        self._snapshots[version_id] = snapshot
```

Update `src/xuanshu/apps/governor.py`:

```python
@dataclass(slots=True)
class GovernorRuntime:
    settings: GovernorRuntimeSettings
    service: GovernorService
    governor_client: GovernorClient
    snapshot_store: RedisSnapshotStore
    last_snapshot: StrategyConfigSnapshot
    published_snapshots: list[StrategyConfigSnapshot] = field(default_factory=list)


def build_snapshot_store(settings: GovernorRuntimeSettings) -> RedisSnapshotStore:
    return RedisSnapshotStore()


def build_governor_runtime() -> GovernorRuntime:
    settings = GovernorRuntimeSettings()
    return GovernorRuntime(
        settings=settings,
        service=build_governor_service(),
        governor_client=build_governor_client(settings),
        snapshot_store=build_snapshot_store(settings),
        last_snapshot=_build_bootstrap_snapshot(),
    )


async def _run_governor(runtime: GovernorRuntime) -> None:
    runtime.last_snapshot = await runtime.service.run_cycle(
        state_summary={"scope": "governor"},
        last_snapshot=runtime.last_snapshot,
        governor_client=runtime.governor_client,
        publish_snapshot=lambda item: (
            runtime.snapshot_store.set_latest_snapshot(item.version_id, item),
            runtime.published_snapshots.append(item),
        ),
    )
    await _wait_forever()
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
git add src/xuanshu/apps/governor.py src/xuanshu/infra/storage/redis_store.py tests/apps/test_governor_app_wiring.py
git commit -m "fix: publish governor snapshots to shared store"
```

## Task 3: Add A Real Notifier Delivery Call Surface

**Files:**
- Modify: `src/xuanshu/apps/notifier.py`
- Modify: `src/xuanshu/infra/notifier/telegram.py`
- Modify: `tests/apps/test_notifier_app_wiring.py`

- [ ] **Step 1: Write the failing notifier delivery test**

Add to `tests/apps/test_notifier_app_wiring.py`:

```python
def test_notifier_runtime_sends_payload_through_adapter(monkeypatch) -> None:
    delivered = []

    class _Adapter:
        async def send_text(self, payload):
            delivered.append(payload.text)

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(notifier_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(notifier_app, "build_notifier_adapter", lambda settings: _Adapter())

    runtime = notifier_app.build_notifier_runtime()
    asyncio.run(notifier_app._run_notifier(runtime))

    assert delivered == ["Notifier runtime started"]
```

- [ ] **Step 2: Run the notifier app test to verify it fails**

Run:

```bash
pytest tests/apps/test_notifier_app_wiring.py -v
```

Expected: FAIL because runtime has no outbound delivery call.

- [ ] **Step 3: Implement a minimal outbound notifier boundary**

Update `src/xuanshu/infra/notifier/telegram.py`:

```python
@dataclass(frozen=True, slots=True)
class TextMessagePayload:
    text: str
    parse_mode: str | None = None


class TelegramNotifier:
    async def send_text(self, payload: TextMessagePayload) -> None:
        return None
```

Update `src/xuanshu/apps/notifier.py`:

```python
@dataclass(frozen=True, slots=True)
class NotifierRuntime:
    settings: NotifierRuntimeSettings
    adapter: TelegramNotifier
    mode: RunMode


def build_notifier_adapter(settings: NotifierRuntimeSettings) -> TelegramNotifier:
    return TelegramNotifier()


def build_notifier_runtime(mode: RunMode | str = RunMode.NORMAL) -> NotifierRuntime:
    settings = NotifierRuntimeSettings()
    return NotifierRuntime(
        settings=settings,
        adapter=build_notifier_adapter(settings),
        mode=mode if isinstance(mode, RunMode) else RunMode(mode),
    )


async def _run_notifier(runtime: NotifierRuntime) -> None:
    await runtime.adapter.send_text(TextMessagePayload(text="Notifier runtime started"))
    await _wait_forever()
```

- [ ] **Step 4: Run the notifier app test to verify it passes**

Run:

```bash
pytest tests/apps/test_notifier_app_wiring.py -v
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run:

```bash
pytest tests -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/xuanshu/apps/notifier.py src/xuanshu/infra/notifier/telegram.py tests/apps/test_notifier_app_wiring.py
git commit -m "fix: add notifier delivery boundary"
```


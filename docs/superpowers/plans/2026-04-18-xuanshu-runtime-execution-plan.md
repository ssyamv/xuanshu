# 玄枢 Runtime Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining runtime-execution gaps so the single-host live-core services do something meaningful at startup: governor publishes snapshots to a shared boundary, notifier performs a real outbound send call, and trader remains alive in a restricted mode when startup gating blocks new risk.

**Architecture:** Keep the skeleton small, but stop treating runtime loops as placeholders. `Governor` will publish snapshots through the Redis boundary that already exists, `Notifier` will call a concrete async send surface on its Telegram adapter, and `Trader` will preserve a live restricted runtime state instead of exiting when startup risk is blocked.

**Tech Stack:** Python 3.12, `asyncio`, `pydantic`, `redis`, `httpx`, `pytest`, `pytest-asyncio`.

---

## Scope Check

This plan is strictly about runtime execution behavior, not feature growth.

In scope:

- Governor shared snapshot publication
- Notifier outbound delivery
- Trader restricted startup runtime behavior

Out of scope:

- Real OpenAI calls
- Real Telegram network calls
- Real Redis persistence
- Full trading/event loops

## Task 1: Publish Governor Snapshots To Shared Store

**Files:**
- Modify: `src/xuanshu/infra/storage/redis_store.py`
- Modify: `src/xuanshu/apps/governor.py`
- Modify: `tests/apps/test_governor_app_wiring.py`

- [ ] **Step 1: Write the failing governor store-publication test**

Add to `tests/apps/test_governor_app_wiring.py`:

```python
def test_governor_runtime_publishes_snapshot_to_shared_store(monkeypatch) -> None:
    stored = []

    class _Store:
        def set_latest_snapshot(self, version_id: str, snapshot) -> None:
            stored.append((version_id, snapshot.version_id))

    async def _noop_wait_forever() -> None:
        return None

    class _Runner:
        async def run(self, state_summary):
            return {
                "version_id": "snap-shared",
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

    assert stored == [("snap-shared", "snap-shared")]
```

- [ ] **Step 2: Run the governor app test to verify it fails**

Run:

```bash
pytest tests/apps/test_governor_app_wiring.py -v
```

Expected: FAIL because runtime does not yet publish through a shared store object.

- [ ] **Step 3: Implement shared snapshot publication**

Update `src/xuanshu/infra/storage/redis_store.py`:

```python
class RedisSnapshotStore:
    def __init__(self) -> None:
        self.snapshots: dict[str, object] = {}

    def set_latest_snapshot(self, version_id: str, snapshot: object) -> None:
        self.snapshots[version_id] = snapshot
```

Update `src/xuanshu/apps/governor.py`:

```python
from xuanshu.infra.storage.redis_store import RedisSnapshotStore


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
git add src/xuanshu/infra/storage/redis_store.py src/xuanshu/apps/governor.py tests/apps/test_governor_app_wiring.py
git commit -m "fix: publish governor snapshots to shared store"
```

## Task 2: Add A Real Notifier Delivery Surface

**Files:**
- Modify: `src/xuanshu/infra/notifier/telegram.py`
- Modify: `src/xuanshu/apps/notifier.py`
- Modify: `tests/apps/test_notifier_app_wiring.py`

- [ ] **Step 1: Write the failing notifier delivery test**

Add to `tests/apps/test_notifier_app_wiring.py`:

```python
def test_notifier_runtime_sends_payload_via_adapter(monkeypatch) -> None:
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

Expected: FAIL because runtime does not yet call an adapter send method.

- [ ] **Step 3: Implement minimal outbound delivery**

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
from xuanshu.infra.notifier.telegram import TelegramNotifier, TextMessagePayload


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

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/infra/notifier/telegram.py src/xuanshu/apps/notifier.py tests/apps/test_notifier_app_wiring.py
git commit -m "fix: add notifier delivery boundary"
```

## Task 3: Keep Trader Alive In Restricted Startup Mode

**Files:**
- Modify: `src/xuanshu/apps/trader.py`
- Modify: `tests/apps/test_trader_app_wiring.py`

- [ ] **Step 1: Write the failing restricted-startup test**

Add to `tests/apps/test_trader_app_wiring.py`:

```python
def test_trader_runtime_stays_alive_when_startup_gating_blocks_opening(monkeypatch) -> None:
    blocked = []

    async def _noop_wait_forever() -> None:
        blocked.append("waited")
        return None

    class _CheckpointProbe:
        def can_open_new_risk(self, checkpoint) -> bool:
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

    assert blocked == ["waited"]
```

- [ ] **Step 2: Run the trader app test to verify it fails**

Run:

```bash
pytest tests/apps/test_trader_app_wiring.py -v
```

Expected: FAIL because `_run_trader()` currently exits early instead of staying alive in restricted mode.

- [ ] **Step 3: Implement restricted startup behavior**

Update `src/xuanshu/apps/trader.py`:

```python
@dataclass(slots=True)
class TraderRuntime:
    settings: TraderRuntimeSettings
    components: TraderComponents
    starting_nav: float
    startup_checkpoint: ExecutionCheckpoint
    opening_allowed: bool = True


async def _run_trader(runtime: TraderRuntime) -> None:
    runtime.opening_allowed = runtime.components.checkpoint_service.can_open_new_risk(runtime.startup_checkpoint)
    await _wait_forever()
```

- [ ] **Step 4: Run the trader app test to verify it passes**

Run:

```bash
pytest tests/apps/test_trader_app_wiring.py -v
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
git add src/xuanshu/apps/trader.py tests/apps/test_trader_app_wiring.py
git commit -m "fix: keep trader alive in restricted startup mode"
```


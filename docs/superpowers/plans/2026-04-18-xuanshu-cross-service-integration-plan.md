# 玄枢 Cross-Service Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining cross-service execution gaps so governor output can influence trader runtime, trader restricted startup mode is externally visible, and notifier actually performs an outbound request instead of discarding payloads.

**Architecture:** Keep the current lightweight single-host design, but make the shared boundaries meaningful. `Governor` will publish snapshots into a process-independent latest-snapshot file store keyed like the existing Redis boundary, `Trader` will read and cache that shared snapshot and publish restricted mode into the same runtime-state boundary, and `Notifier` will emit a concrete HTTP request through the Telegram adapter. This is still a minimal runtime, not the full live engine.

**Tech Stack:** Python 3.12, `asyncio`, `json`, `httpx`, `pydantic`, `pytest`, `pytest-asyncio`.

---

## Scope Check

This plan is only about shared state and outbound runtime execution.

In scope:

- Governor snapshot publication to a shared boundary
- Trader snapshot consumption and restricted mode publication
- Telegram adapter outbound request path

Out of scope:

- Real Redis network integration
- Real long-running trading loop
- Real OpenAI execution beyond current client boundary
- End-to-end docker orchestration tests

## Task 1: Publish And Consume Snapshots Through A Shared File-Backed Store

**Files:**
- Modify: `src/xuanshu/infra/storage/redis_store.py`
- Modify: `src/xuanshu/apps/governor.py`
- Modify: `src/xuanshu/apps/trader.py`
- Modify: `tests/apps/test_governor_app_wiring.py`
- Modify: `tests/apps/test_trader_app_wiring.py`

- [ ] **Step 1: Write the failing shared-snapshot tests**

Add to `tests/apps/test_governor_app_wiring.py`:

```python
def test_governor_runtime_publishes_snapshot_to_shared_file_store(monkeypatch, tmp_path) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)

    class _Runner:
        async def run(self, state_summary):
            return {
                "version_id": "snap-file",
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

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setenv("XUANSHU_SHARED_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))

    runtime = governor_app.build_governor_runtime()
    asyncio.run(governor_app._run_governor(runtime))

    stored = runtime.snapshot_store.get_latest_snapshot()
    assert stored is not None
    assert stored.version_id == "snap-file"
```

Add to `tests/apps/test_trader_app_wiring.py`:

```python
def test_trader_runtime_reads_latest_snapshot_from_shared_store(monkeypatch, tmp_path) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_SHARED_STATE_DIR", str(tmp_path))

    runtime = trader_app.build_trader_runtime()
    runtime.snapshot_store.set_latest_snapshot(
        "snap-shared",
        runtime.startup_snapshot.model_copy(update={"version_id": "snap-shared"}),
    )

    assert runtime.snapshot_store.get_latest_snapshot().version_id == "snap-shared"
```

- [ ] **Step 2: Run the app tests to verify they fail**

Run:

```bash
pytest tests/apps/test_governor_app_wiring.py tests/apps/test_trader_app_wiring.py -v
```

Expected: FAIL because the current store is not shared/persistent and trader does not consume it.

- [ ] **Step 3: Implement a file-backed shared snapshot store and trader consumption**

Update `src/xuanshu/infra/storage/redis_store.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from xuanshu.contracts.strategy import StrategyConfigSnapshot


class SnapshotStore(Protocol):
    def set_latest_snapshot(self, version_id: str, snapshot: StrategyConfigSnapshot) -> None:
        ...

    def get_latest_snapshot(self) -> StrategyConfigSnapshot | None:
        ...


class RedisSnapshotStore:
    def __init__(self, state_dir: str | Path = ".xuanshu-state") -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.state_dir / "latest_strategy_snapshot.json"

    def set_latest_snapshot(self, version_id: str, snapshot: StrategyConfigSnapshot) -> None:
        self._path.write_text(snapshot.model_dump_json(), encoding="utf-8")

    def get_latest_snapshot(self) -> StrategyConfigSnapshot | None:
        if not self._path.exists():
            return None
        return StrategyConfigSnapshot.model_validate_json(self._path.read_text(encoding="utf-8"))
```

Update `src/xuanshu/apps/governor.py` and `src/xuanshu/apps/trader.py` so both use the same `XUANSHU_SHARED_STATE_DIR`-backed store, and `TraderRuntime` carries:

```python
snapshot_store: SnapshotStore
startup_snapshot: StrategyConfigSnapshot
```

with trader loading the latest snapshot during startup:

```python
latest_snapshot = runtime.snapshot_store.get_latest_snapshot()
if latest_snapshot is not None:
    runtime.startup_snapshot = latest_snapshot
```

- [ ] **Step 4: Run the app tests to verify they pass**

Run:

```bash
pytest tests/apps/test_governor_app_wiring.py tests/apps/test_trader_app_wiring.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/infra/storage/redis_store.py src/xuanshu/apps/governor.py src/xuanshu/apps/trader.py tests/apps/test_governor_app_wiring.py tests/apps/test_trader_app_wiring.py
git commit -m "fix: share snapshots across governor and trader"
```

## Task 2: Publish Restricted Startup Mode To Shared Runtime State

**Files:**
- Modify: `src/xuanshu/infra/storage/redis_store.py`
- Modify: `src/xuanshu/apps/trader.py`
- Modify: `tests/apps/test_trader_app_wiring.py`

- [ ] **Step 1: Write the failing restricted-mode publication test**

Add to `tests/apps/test_trader_app_wiring.py`:

```python
def test_trader_runtime_publishes_restricted_mode_when_opening_blocked(monkeypatch, tmp_path) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_SHARED_STATE_DIR", str(tmp_path))

    async def _noop_wait_forever() -> None:
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

    assert runtime.current_mode == RunMode.REDUCE_ONLY
    assert runtime.runtime_store.get_run_mode() == RunMode.REDUCE_ONLY
```

- [ ] **Step 2: Run the trader app test to verify it fails**

Run:

```bash
pytest tests/apps/test_trader_app_wiring.py -v
```

Expected: FAIL because runtime mode is not published.

- [ ] **Step 3: Implement runtime-mode publication**

Update `src/xuanshu/infra/storage/redis_store.py`:

```python
class RuntimeStateStore:
    def __init__(self, state_dir: str | Path = ".xuanshu-state") -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.state_dir / "runtime_mode.txt"

    def set_run_mode(self, mode: str) -> None:
        self._path.write_text(mode, encoding="utf-8")

    def get_run_mode(self) -> str | None:
        if not self._path.exists():
            return None
        return self._path.read_text(encoding="utf-8").strip() or None
```

Update `src/xuanshu/apps/trader.py`:

```python
current_mode: RunMode = RunMode.NORMAL
runtime_store: RuntimeStateStore
...
if not runtime.opening_allowed:
    runtime.current_mode = RunMode.REDUCE_ONLY
runtime.runtime_store.set_run_mode(runtime.current_mode.value)
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
git add src/xuanshu/infra/storage/redis_store.py src/xuanshu/apps/trader.py tests/apps/test_trader_app_wiring.py
git commit -m "fix: publish restricted trader startup mode"
```

## Task 3: Make Telegram Adapter Perform A Real HTTP Send Call

**Files:**
- Modify: `src/xuanshu/infra/notifier/telegram.py`
- Modify: `tests/apps/test_notifier_app_wiring.py`

- [ ] **Step 1: Write the failing telegram-send test**

Add to `tests/apps/test_notifier_app_wiring.py`:

```python
@pytest.mark.asyncio
async def test_telegram_notifier_send_text_makes_http_request(monkeypatch) -> None:
    calls = []

    class _Client:
        async def post(self, url, json):
            calls.append((url, json))

    notifier = TelegramNotifier(
        bot_token=SecretStr("token"),
        chat_id="123",
        client=_Client(),
    )

    await notifier.send_text("hello")

    assert calls == [(
        "https://api.telegram.org/bottoken/sendMessage",
        {"chat_id": "123", "text": "hello"},
    )]
```

- [ ] **Step 2: Run the notifier test to verify it fails**

Run:

```bash
pytest tests/apps/test_notifier_app_wiring.py -v
```

Expected: FAIL because `send_text()` still drops the payload.

- [ ] **Step 3: Implement a minimal HTTP send path**

Update `src/xuanshu/infra/notifier/telegram.py`:

```python
from dataclasses import dataclass, field

import httpx
from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class TextMessagePayload:
    text: str
    parse_mode: str | None = None


def render_text_message(text: str) -> TextMessagePayload:
    return TextMessagePayload(text=text)


@dataclass(slots=True)
class TelegramNotifier:
    bot_token: SecretStr
    chat_id: str
    client: httpx.AsyncClient | object = field(default_factory=httpx.AsyncClient)

    async def send_text(self, text: str) -> None:
        payload = render_text_message(text)
        body = {"chat_id": self.chat_id, "text": payload.text}
        if payload.parse_mode is not None:
            body["parse_mode"] = payload.parse_mode
        await self.client.post(
            f"https://api.telegram.org/bot{self.bot_token.get_secret_value()}/sendMessage",
            json=body,
        )
```

- [ ] **Step 4: Run the notifier test to verify it passes**

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
git add src/xuanshu/infra/notifier/telegram.py tests/apps/test_notifier_app_wiring.py
git commit -m "fix: make notifier send telegram requests"
```


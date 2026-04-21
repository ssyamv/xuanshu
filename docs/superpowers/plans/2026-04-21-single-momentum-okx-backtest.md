# Single Momentum OKX Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the active strategy path with one fixed BTC 1H long-only momentum strategy selected by OKX historical backtesting.

**Architecture:** Keep the existing trader execution, state, recovery, and risk infrastructure. Add a focused momentum backtest module and CLI, write one fixed `StrategyConfigSnapshot` JSON file, and make trader startup prefer that fixed snapshot over governor-published dynamic research when configured.

**Tech Stack:** Python 3.12, Pydantic Settings, httpx/OKX REST, pytest, Docker Compose, SSH

---

## File Structure

- `src/xuanshu/momentum/backtest.py`: pure momentum strategy parameter evaluation, selection gates, snapshot conversion.
- `src/xuanshu/momentum/okx_history.py`: OKX candle pagination and normalization using `OkxRestClient.fetch_history_candles`.
- `src/xuanshu/apps/momentum_backtest.py`: CLI entrypoint for fetching OKX history, running the grid, and writing a fixed strategy snapshot.
- `src/xuanshu/config/settings.py`: add fixed strategy file path and backtest settings.
- `src/xuanshu/apps/trader.py`: load configured fixed snapshot before Redis/governor snapshot lookup.
- `.env.example`, `.env.prod.example`, `docker-compose.yml`: expose fixed strategy snapshot path and protected production defaults.
- `tests/momentum/test_momentum_backtest.py`: pure backtest and selection tests.
- `tests/momentum/test_okx_history.py`: candle normalization/pagination tests.
- `tests/apps/test_momentum_backtest_app.py`: CLI write/no-write behavior tests.
- `tests/apps/test_trader_app_wiring.py`: trader fixed snapshot startup tests.

## Task 1: Put Production Into Explicit Protected Mode

**Files:**
- Remote only: `/opt/xuanshu/.env.prod`

- [ ] **Step 1: Inspect remote runtime values**

Run:

```bash
python3 /Users/chenqi/.codex/skills/ssh-skill/scripts/ssh_execute.py xuanshu-prod-01 "cd /opt/xuanshu && grep -E '^(XUANSHU_DEFAULT_RUN_MODE|XUANSHU_OKX_ACCOUNT_MODE)=' .env.prod || true" --timeout 20 --no-daemon
```

Expected: shows current values or no matching lines.

- [ ] **Step 2: Set explicit halted mode**

Run:

```bash
python3 /Users/chenqi/.codex/skills/ssh-skill/scripts/ssh_execute.py xuanshu-prod-01 "cd /opt/xuanshu && cp .env.prod .env.prod.pre-single-momentum && if grep -q '^XUANSHU_DEFAULT_RUN_MODE=' .env.prod; then sed -i 's/^XUANSHU_DEFAULT_RUN_MODE=.*/XUANSHU_DEFAULT_RUN_MODE=halted/' .env.prod; else printf '\nXUANSHU_DEFAULT_RUN_MODE=halted\n' >> .env.prod; fi" --timeout 20 --no-daemon
```

Expected: command exits 0 and creates `.env.prod.pre-single-momentum`.

- [ ] **Step 3: Restart compose in protected mode**

Run:

```bash
python3 /Users/chenqi/.codex/skills/ssh-skill/scripts/ssh_execute.py xuanshu-prod-01 "cd /opt/xuanshu && docker compose --env-file .env.prod up -d" --timeout 120 --no-daemon
```

Expected: compose services recreate or remain up without errors.

- [ ] **Step 4: Verify trader sees halted mode**

Run:

```bash
python3 /Users/chenqi/.codex/skills/ssh-skill/scripts/ssh_execute.py xuanshu-prod-01 "cd /opt/xuanshu && docker compose --env-file .env.prod exec -T trader env | grep '^XUANSHU_DEFAULT_RUN_MODE='" --timeout 30 --no-daemon
```

Expected: `XUANSHU_DEFAULT_RUN_MODE=halted`.

## Task 2: Add Pure Momentum Backtest Model

**Files:**
- Create: `src/xuanshu/momentum/__init__.py`
- Create: `src/xuanshu/momentum/backtest.py`
- Test: `tests/momentum/test_momentum_backtest.py`

- [ ] **Step 1: Write failing pure backtest tests**

Create tests that define deterministic rising/falling candles and assert:

```python
from datetime import UTC, datetime, timedelta

from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.momentum.backtest import (
    MomentumBacktestConfig,
    MomentumParameterSet,
    build_momentum_snapshot,
    evaluate_momentum_candidate,
    select_best_candidate,
)


def _rows(closes: list[float]) -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        {
            "timestamp": start + timedelta(hours=index),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
        }
        for index, close in enumerate(closes)
    ]


def test_evaluate_momentum_candidate_trades_positive_breakouts() -> None:
    params = MomentumParameterSet(
        lookback=3,
        stop_loss_bps=100,
        take_profit_bps=200,
        max_hold_minutes=180,
    )

    result = evaluate_momentum_candidate(params, _rows([100, 101, 102, 104, 107, 109, 112, 114, 117]))

    assert result.trade_count >= 1
    assert result.return_percent > 0
    assert result.profit_factor > 1


def test_select_best_candidate_rejects_when_gates_fail() -> None:
    config = MomentumBacktestConfig(
        min_trade_count=30,
        max_drawdown_percent=5.0,
        risk_fraction=0.25,
    )
    bad_result = evaluate_momentum_candidate(
        MomentumParameterSet(lookback=3, stop_loss_bps=100, take_profit_bps=200, max_hold_minutes=180),
        _rows([100, 99, 98, 97, 96, 95, 94]),
    )

    selected = select_best_candidate([bad_result], config=config)

    assert selected is None


def test_build_momentum_snapshot_serializes_single_fixed_strategy() -> None:
    config = MomentumBacktestConfig(
        min_trade_count=1,
        max_drawdown_percent=20.0,
        risk_fraction=0.25,
    )
    result = evaluate_momentum_candidate(
        MomentumParameterSet(lookback=3, stop_loss_bps=100, take_profit_bps=200, max_hold_minutes=180),
        _rows([100, 101, 102, 104, 107, 109, 112, 114, 117]),
    )

    snapshot = build_momentum_snapshot(
        selected=result,
        symbol="BTC-USDT-SWAP",
        generated_at=datetime(2026, 1, 2, tzinfo=UTC),
        config=config,
    )

    assert snapshot.symbol_whitelist == ["BTC-USDT-SWAP"]
    assert snapshot.market_mode == RunMode.HALTED
    assert snapshot.approval_state == ApprovalState.APPROVED
    assert snapshot.strategy_enable_flags == {"momentum": True}
    assert snapshot.symbol_strategy_bindings["BTC-USDT-SWAP"].strategy_def_id.startswith("momentum-btc-usdt-swap-")
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run pytest tests/momentum/test_momentum_backtest.py -v
```

Expected: FAIL because `xuanshu.momentum.backtest` does not exist.

- [ ] **Step 3: Implement minimal pure backtest**

Implement:

```python
@dataclass(frozen=True, slots=True)
class MomentumParameterSet:
    lookback: int
    stop_loss_bps: int
    take_profit_bps: int
    max_hold_minutes: int

@dataclass(frozen=True, slots=True)
class MomentumBacktestConfig:
    min_trade_count: int = 30
    max_drawdown_percent: float = 20.0
    risk_fraction: float = 0.25

@dataclass(frozen=True, slots=True)
class MomentumBacktestResult:
    parameters: MomentumParameterSet
    sample_count: int
    trade_count: int
    return_percent: float
    max_drawdown_percent: float
    win_rate: float
    profit_factor: float
    stability_score: float
```

Use sorted timestamp/close rows, enter long when `current_close > lookback_close`, exit on stop loss, take profit, max hold, or end of data. `select_best_candidate` filters by gates and sorts by `(stability_score, return_percent, profit_factor)`. `build_momentum_snapshot` returns a halted `StrategyConfigSnapshot` with one `ApprovedStrategyBinding`.

- [ ] **Step 4: Run test to verify GREEN**

Run:

```bash
uv run pytest tests/momentum/test_momentum_backtest.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/momentum tests/momentum/test_momentum_backtest.py
git commit -m "feat: add fixed momentum backtest model"
```

## Task 3: Add OKX Historical Candle Loader

**Files:**
- Create: `src/xuanshu/momentum/okx_history.py`
- Test: `tests/momentum/test_okx_history.py`

- [ ] **Step 1: Write failing OKX history tests**

Create tests with a fake client:

```python
from datetime import UTC, datetime

import pytest

from xuanshu.momentum.okx_history import fetch_okx_history_rows, normalize_okx_candle


class _FakeOkxClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def fetch_history_candles(self, symbol: str, *, bar: str, after: str | None = None, before: str | None = None, limit: int = 100):
        self.calls.append({"symbol": symbol, "bar": bar, "after": after, "before": before, "limit": limit})
        if len(self.calls) == 1:
            return [
                {"ts": "1700003600000", "open": "101", "high": "102", "low": "100", "close": "101.5"},
                {"ts": "1700000000000", "open": "100", "high": "101", "low": "99", "close": "100.5"},
            ]
        return []


def test_normalize_okx_candle_parses_timestamp_and_prices() -> None:
    row = normalize_okx_candle({"ts": "1700000000000", "open": "100", "high": "101", "low": "99", "close": "100.5"})

    assert row["timestamp"] == datetime.fromtimestamp(1700000000, tz=UTC)
    assert row["close"] == 100.5


@pytest.mark.asyncio
async def test_fetch_okx_history_rows_returns_sorted_unique_rows() -> None:
    client = _FakeOkxClient()

    rows = await fetch_okx_history_rows(client, symbol="BTC-USDT-SWAP", bar="1H", limit=200)

    assert [row["close"] for row in rows] == [100.5, 101.5]
    assert client.calls[0]["limit"] == 100
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run pytest tests/momentum/test_okx_history.py -v
```

Expected: FAIL because `xuanshu.momentum.okx_history` does not exist.

- [ ] **Step 3: Implement loader**

Implement `normalize_okx_candle(row)` and `fetch_okx_history_rows(client, symbol, bar, limit)` using repeated `fetch_history_candles(..., limit=min(100, remaining), after=oldest_ts)` calls. Sort ascending by timestamp, drop duplicate timestamps, and reject non-positive or non-finite OHLC values.

- [ ] **Step 4: Run test to verify GREEN**

Run:

```bash
uv run pytest tests/momentum/test_okx_history.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/momentum/okx_history.py tests/momentum/test_okx_history.py
git commit -m "feat: fetch OKX momentum history"
```

## Task 4: Add Momentum Backtest CLI

**Files:**
- Create: `src/xuanshu/apps/momentum_backtest.py`
- Modify: `src/xuanshu/config/settings.py`
- Test: `tests/apps/test_momentum_backtest_app.py`

- [ ] **Step 1: Write failing CLI tests**

Create tests that monkeypatch history fetching and assert atomic write/no-write:

```python
import json
from datetime import UTC, datetime, timedelta

import pytest

import xuanshu.apps.momentum_backtest as app


def _rows() -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    closes = [100 + index * 2 for index in range(80)]
    return [
        {"timestamp": start + timedelta(hours=index), "open": close, "high": close + 1, "low": close - 1, "close": close}
        for index, close in enumerate(closes)
    ]


@pytest.mark.asyncio
async def test_run_backtest_writes_fixed_snapshot(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "active_strategy.json"

    async def fake_fetch(*args, **kwargs):
        return _rows()

    monkeypatch.setattr(app, "fetch_okx_history_rows", fake_fetch)

    exit_code = await app.run_backtest(["--output", str(output_path), "--limit", "80", "--min-trades", "1"])

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["symbol_whitelist"] == ["BTC-USDT-SWAP"]
    assert payload["strategy_enable_flags"] == {"momentum": True}


@pytest.mark.asyncio
async def test_run_backtest_does_not_write_when_no_candidate_passes(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "active_strategy.json"

    async def fake_fetch(*args, **kwargs):
        return _rows()[:10]

    monkeypatch.setattr(app, "fetch_okx_history_rows", fake_fetch)

    exit_code = await app.run_backtest(["--output", str(output_path), "--limit", "10", "--min-trades", "30"])

    assert exit_code == 2
    assert not output_path.exists()
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run pytest tests/apps/test_momentum_backtest_app.py -v
```

Expected: FAIL because `xuanshu.apps.momentum_backtest` does not exist.

- [ ] **Step 3: Implement CLI**

Implement:

- `parse_args(argv)` with defaults `--symbol BTC-USDT-SWAP`, `--bar 1H`, `--limit 4320`, `--output configs/active_strategy.json`, `--min-trades 30`, `--max-drawdown 20`.
- `run_backtest(argv)` creates unauthenticated `OkxRestClient(base_url="https://www.okx.com", api_key="")`, fetches rows, evaluates the fixed parameter grid, writes `StrategyConfigSnapshot.model_dump_json(indent=2)` to a temporary file, then replaces the output path.
- `main()` calls `asyncio.run(run_backtest())`.

- [ ] **Step 4: Run test to verify GREEN**

Run:

```bash
uv run pytest tests/apps/test_momentum_backtest_app.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/apps/momentum_backtest.py src/xuanshu/config/settings.py tests/apps/test_momentum_backtest_app.py
git commit -m "feat: add momentum backtest command"
```

## Task 5: Load Fixed Strategy Snapshot In Trader

**Files:**
- Modify: `src/xuanshu/config/settings.py`
- Modify: `src/xuanshu/apps/trader.py`
- Modify: `.env.example`
- Modify: `.env.prod.example`
- Modify: `docker-compose.yml`
- Test: `tests/apps/test_trader_app_wiring.py`

- [ ] **Step 1: Write failing trader fixed snapshot tests**

Add tests:

```python
def test_trader_runtime_loads_fixed_strategy_snapshot_before_redis(monkeypatch, tmp_path) -> None:
    _set_required_settings_env(monkeypatch)
    fixed_path = tmp_path / "active_strategy.json"
    generated_at = datetime(2026, 1, 1, tzinfo=UTC)
    snapshot = trader_app.StrategyConfigSnapshot(
        version_id="fixed-momentum-test",
        generated_at=generated_at,
        effective_from=generated_at,
        expires_at=generated_at + trader_app.timedelta(days=3650),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"momentum": True},
        risk_multiplier=0.5,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.HALTED,
        approval_state=ApprovalState.APPROVED,
        source_reason="fixed momentum backtest",
        ttl_sec=315360000,
    )
    fixed_path.write_text(snapshot.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH", str(fixed_path))

    runtime = trader_app.build_trader_runtime()

    assert runtime.startup_snapshot.version_id == "fixed-momentum-test"
    assert runtime.current_mode == RunMode.HALTED
    assert runtime.startup_snapshot.strategy_enable_flags == {"momentum": True}


def test_trader_runtime_rejects_malformed_fixed_strategy_snapshot(monkeypatch, tmp_path) -> None:
    _set_required_settings_env(monkeypatch)
    fixed_path = tmp_path / "active_strategy.json"
    fixed_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setenv("XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH", str(fixed_path))

    with pytest.raises(ValueError, match="fixed strategy snapshot"):
        trader_app.build_trader_runtime()


def test_deployment_contract_lists_fixed_strategy_snapshot_path() -> None:
    assert "XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH=" in Path(".env.example").read_text(encoding="utf-8")
    assert "XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH=" in Path(".env.prod.example").read_text(encoding="utf-8")
    assert "XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH:" in Path("docker-compose.yml").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run pytest tests/apps/test_trader_app_wiring.py::test_trader_runtime_loads_fixed_strategy_snapshot_before_redis tests/apps/test_trader_app_wiring.py::test_trader_runtime_rejects_malformed_fixed_strategy_snapshot tests/apps/test_trader_app_wiring.py::test_deployment_contract_lists_fixed_strategy_snapshot_path -v
```

Expected: FAIL because the setting and loader do not exist.

- [ ] **Step 3: Implement fixed snapshot loading**

Add to `TraderRuntimeSettings`:

```python
fixed_strategy_snapshot_path: str | None = Field(default=None)
```

Add helper in `trader.py`:

```python
def _load_fixed_strategy_snapshot(path: str | None) -> StrategyConfigSnapshot | None:
    if path is None or not path.strip():
        return None
    try:
        return StrategyConfigSnapshot.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid fixed strategy snapshot: {path}") from exc
```

In `build_trader_runtime`, load fixed snapshot before Redis snapshot lookup. If fixed snapshot exists, use it and skip Redis latest snapshot. Still apply `_more_restrictive_mode(settings.default_run_mode, fixed_snapshot.market_mode)`.

Update env files and compose with `XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH`.

- [ ] **Step 4: Run test to verify GREEN**

Run:

```bash
uv run pytest tests/apps/test_trader_app_wiring.py::test_trader_runtime_loads_fixed_strategy_snapshot_before_redis tests/apps/test_trader_app_wiring.py::test_trader_runtime_rejects_malformed_fixed_strategy_snapshot tests/apps/test_trader_app_wiring.py::test_deployment_contract_lists_fixed_strategy_snapshot_path -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/config/settings.py src/xuanshu/apps/trader.py .env.example .env.prod.example docker-compose.yml tests/apps/test_trader_app_wiring.py
git commit -m "feat: load fixed strategy snapshot in trader"
```

## Task 6: Full Verification And Manual OKX Fetch Smoke

**Files:**
- No planned source changes.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/momentum tests/apps/test_momentum_backtest_app.py tests/apps/test_trader_app_wiring.py -v
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
uv run pytest
```

Expected: PASS.

- [ ] **Step 3: Run live OKX history smoke without writing active strategy**

Run:

```bash
uv run python -m xuanshu.apps.momentum_backtest --symbol BTC-USDT-SWAP --bar 1H --limit 300 --min-trades 1 --output /tmp/xuanshu-active-strategy-smoke.json
```

Expected: exits 0 if a candidate passes, or exits 2 if no candidate passes. Network or OKX failures must show a clear non-zero failure. Do not deploy this file automatically.

- [ ] **Step 4: Inspect git status**

Run:

```bash
git status --short
```

Expected: clean after commits, or only intentional uncommitted files if final edits are pending.

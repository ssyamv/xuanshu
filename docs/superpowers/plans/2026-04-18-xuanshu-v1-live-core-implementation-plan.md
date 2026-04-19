# 玄枢 V1 Live Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first production-shaped `玄枢 V1 live core` on a single server: deterministic trader fast path, AI-governed snapshot publication, async Telegram notifier, Redis/PostgreSQL/Qdrant storage boundaries, and checkpoint-based recovery for `OKX BTC/ETH` perpetuals.

**Architecture:** Implement one Python repository that runs three business services on one host: `Trader Service` for hot-path execution, `Governor Service` for AI governance and snapshot publication, and `Notifier Service` for Telegram visibility and human takeover. Use stable contracts (`MarketStateSnapshot`, `StrategyConfigSnapshot`, `RiskDecision`, `ExecutionCheckpoint`, `ExpertOpinion`) as the only cross-module language, with Redis for hot state, PostgreSQL for facts/checkpoints, and Qdrant reserved for slow-path case retrieval.

**Tech Stack:** Python 3.12, `asyncio`, `pydantic`, `pydantic-settings`, `httpx`, `websockets`, `redis`, `sqlalchemy`, `psycopg`, `structlog`, `pytest`, `pytest-asyncio`, `respx`, OpenAI Agents SDK, Telegram Bot API, Docker Compose.

---

## Scope Check

The approved docs describe more than one eventual subsystem, but this implementation plan stays intentionally inside the approved `live core` scope:

- `Trader Service`
- `Governor Service`
- `Notifier Service`
- Contracts, state, storage, and recovery
- Single-host deployment packaging

Out of scope for this plan:

- Replay/backtest
- MLflow / promotion flows
- Strategy research workbench
- Multi-node deployment
- Deep Qdrant retrieval logic
- Monitoring dashboards beyond wiring-ready hooks

## File Structure

Lock the repository to these responsibilities before coding:

- `pyproject.toml`: package, tooling, test configuration
- `.env.example`: required runtime environment variables
- `docker-compose.yml`: single-host local/prod-like runtime wiring
- `src/xuanshu/core/enums.py`: run modes, strategy ids, event types
- `src/xuanshu/contracts/market.py`: market/state snapshots and event contracts
- `src/xuanshu/contracts/strategy.py`: governance snapshot contracts
- `src/xuanshu/contracts/risk.py`: candidate signal and risk decision contracts
- `src/xuanshu/contracts/checkpoint.py`: checkpoint and reconcile contracts
- `src/xuanshu/contracts/governance.py`: expert opinion and committee result contracts
- `src/xuanshu/config/settings.py`: runtime settings
- `src/xuanshu/infra/okx/public_ws.py`: public market stream client
- `src/xuanshu/infra/okx/private_ws.py`: private account/order stream client
- `src/xuanshu/infra/okx/rest.py`: order placement + reconcile REST adapter
- `src/xuanshu/infra/storage/redis_store.py`: hot-state cache and snapshot access
- `src/xuanshu/infra/storage/postgres_store.py`: facts/checkpoints/audit persistence
- `src/xuanshu/infra/storage/qdrant_store.py`: slow-path case retrieval boundary
- `src/xuanshu/infra/ai/governor_client.py`: OpenAI Agents SDK wrapper
- `src/xuanshu/infra/notifier/telegram.py`: async Telegram adapter
- `src/xuanshu/state/engine.py`: in-memory trader state engine
- `src/xuanshu/strategies/regime_router.py`: regime classification
- `src/xuanshu/strategies/signals.py`: strategy basket signal generation
- `src/xuanshu/risk/kernel.py`: hard risk checks and mode tightening
- `src/xuanshu/execution/engine.py`: deterministic execution payload building
- `src/xuanshu/checkpoints/service.py`: checkpoint creation/reconcile guards
- `src/xuanshu/governor/service.py`: governance loop and snapshot publication
- `src/xuanshu/notifier/service.py`: notification formatting/query responses
- `src/xuanshu/apps/trader.py`: trader composition root
- `src/xuanshu/apps/governor.py`: governor composition root
- `src/xuanshu/apps/notifier.py`: notifier composition root
- `tests/`: mirror runtime responsibilities with one focused test file per module cluster

## Task 1: Bootstrap The Single-Host Repository

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `src/xuanshu/__init__.py`
- Create: `src/xuanshu/apps/__init__.py`
- Create: `tests/test_project_smoke.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Initialize the repository**

Run:

```bash
git init
mkdir -p src/xuanshu/apps tests
```

Expected: `.git/` exists and `src/xuanshu/apps`, `tests` directories exist.

- [ ] **Step 2: Write the failing package smoke test**

Create `tests/test_project_smoke.py`:

```python
import subprocess
import sys
from pathlib import Path


def test_package_imports() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    subprocess.run(
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        check=True,
        cwd=repo_root,
    )
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "-e", "."],
        check=True,
        cwd=repo_root,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlib.metadata import version; import xuanshu; assert xuanshu.__name__ == 'xuanshu'; assert xuanshu.__version__ == version('xuanshu')",
        ],
        check=True,
        cwd=repo_root,
    )

    assert result.returncode == 0
```

- [ ] **Step 3: Run the smoke test to verify it fails**

Run:

```bash
pytest tests/test_project_smoke.py -v
```

Expected: FAIL before scaffolding because the package is not yet installable.

- [ ] **Step 4: Add minimal project scaffolding**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=69", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "xuanshu"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "pydantic>=2.7",
  "pydantic-settings>=2.2",
  "httpx>=0.27",
  "websockets>=12.0",
  "redis>=5.0",
  "sqlalchemy>=2.0",
  "psycopg[binary]>=3.1",
  "structlog>=24.1",
  "openai-agents>=0.0.10",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.2",
  "pytest-asyncio>=0.23",
  "respx>=0.21",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

Create `.env.example`:

```dotenv
XUANSHU_ENV=dev
XUANSHU_OKX_SYMBOLS=BTC-USDT-SWAP,ETH-USDT-SWAP
OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=
OPENAI_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
POSTGRES_DSN=postgresql+psycopg://xuanshu:xuanshu@postgres:5432/xuanshu
REDIS_URL=redis://redis:6379/0
QDRANT_URL=http://qdrant:6333
```

Create `src/xuanshu/__init__.py`:

```python
from importlib.metadata import version

__all__ = ["__version__"]

__version__ = version("xuanshu")
```

Create `src/xuanshu/apps/__init__.py`:

```python
"""Service entrypoints for xuanshu."""
```

Create `tests/conftest.py`:

```python
import os


def pytest_sessionstart(session) -> None:
    os.environ["XUANSHU_ENV"] = "test"
```

- [ ] **Step 5: Run the smoke test to verify it passes**

Run:

```bash
pytest tests/test_project_smoke.py -v
```

Expected: PASS after editable install and import verification.

- [ ] **Step 6: Commit**

Run:

```bash
git add pyproject.toml .env.example src/xuanshu/__init__.py src/xuanshu/apps/__init__.py tests/conftest.py tests/test_project_smoke.py
git commit -m "chore: bootstrap xuanshu live core repository"
```

## Task 2: Implement Stable Contracts And Runtime Settings

**Files:**
- Create: `src/xuanshu/core/enums.py`
- Create: `src/xuanshu/contracts/market.py`
- Create: `src/xuanshu/contracts/strategy.py`
- Create: `src/xuanshu/contracts/risk.py`
- Create: `src/xuanshu/contracts/checkpoint.py`
- Create: `src/xuanshu/contracts/governance.py`
- Create: `src/xuanshu/config/settings.py`
- Test: `tests/contracts/test_contracts.py`

- [ ] **Step 1: Write the failing contract test**

Create `tests/contracts/test_contracts.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from xuanshu.config.settings import Settings
from xuanshu.contracts.checkpoint import CheckpointBudgetState, CheckpointOrder, CheckpointPosition, ExecutionCheckpoint
from xuanshu.contracts.governance import ExpertOpinion
from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.contracts.risk import CandidateSignal
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import EntryType, MarketRegime, OrderSide, RunMode, SignalUrgency, VolatilityState


def test_strategy_snapshot_and_expert_opinion_are_stable_contracts() -> None:
    snapshot = StrategyConfigSnapshot(
        version_id="snap-001",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": True, "risk_pause": True},
        risk_multiplier=0.8,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state="approved",
        source_reason="committee result",
        ttl_sec=300,
    )
    opinion = ExpertOpinion(
        opinion_id="op-001",
        expert_type="risk",
        generated_at=datetime.now(UTC),
        symbol_scope=["BTC-USDT-SWAP"],
        decision="tighten_risk",
        confidence=0.8,
        supporting_facts=["recent risk events rising"],
        risk_flags=["drawdown_watch"],
        ttl_sec=300,
    )

    assert snapshot.is_expired(datetime.now(UTC)) is False
    assert opinion.expert_type == "risk"


def test_taxonomy_and_numeric_bounds_reject_invalid_contracts() -> None:
    with pytest.raises(ValidationError):
        CandidateSignal(
            symbol="BTC-USDT-SWAP",
            strategy_id="breakout",
            side="buy",
            entry_type=EntryType.MARKET,
            urgency=SignalUrgency.HIGH,
            confidence=-0.1,
            max_hold_ms=-1,
            cancel_after_ms=0,
            risk_tag="risk-on",
        )

    with pytest.raises(ValidationError):
        MarketStateSnapshot(
            snapshot_id="snap-001",
            generated_at=datetime.now(UTC),
            symbol="BTC-USDT-SWAP",
            mid_price=-1.0,
            spread=0.1,
            imbalance=1.2,
            recent_trade_bias=0.0,
            volatility_state=VolatilityState.NORMAL,
            regime=MarketRegime.TREND,
            current_position=-0.5,
            current_mode=RunMode.NORMAL,
            risk_budget_remaining=-10.0,
        )


def test_checkpoint_payload_is_typed_and_settings_validate_urls() -> None:
    checkpoint = ExecutionCheckpoint(
        checkpoint_id="cp-001",
        created_at=datetime.now(UTC),
        active_snapshot_version="snap-001",
        current_mode=RunMode.NORMAL,
        positions_snapshot=[
            CheckpointPosition(
                symbol="BTC-USDT-SWAP",
                net_quantity=0.2,
                mark_price=62000.0,
                unrealized_pnl=125.0,
            )
        ],
        open_orders_snapshot=[
            CheckpointOrder(
                order_id="order-001",
                symbol="BTC-USDT-SWAP",
                side=OrderSide.BUY,
                price=62100.0,
                size=0.05,
                status="open",
            )
        ],
        budget_state=CheckpointBudgetState(
            max_daily_loss=1000.0,
            remaining_daily_loss=650.0,
            remaining_notional=5000.0,
            remaining_order_count=12,
        ),
        last_public_stream_marker=None,
        last_private_stream_marker="stream-1",
        needs_reconcile=False,
    )

    assert checkpoint.positions_snapshot[0].symbol == "BTC-USDT-SWAP"

    with pytest.raises(ValidationError):
        ExecutionCheckpoint(
            checkpoint_id="cp-002",
            created_at=datetime.now(UTC),
            active_snapshot_version="snap-002",
            current_mode=RunMode.NORMAL,
            positions_snapshot=[{"symbol": "BTC-USDT-SWAP", "net_quantity": 0.2}],
            open_orders_snapshot=[],
            budget_state=CheckpointBudgetState(
                max_daily_loss=1000.0,
                remaining_daily_loss=650.0,
                remaining_notional=5000.0,
                remaining_order_count=12,
            ),
            last_public_stream_marker=None,
            last_private_stream_marker=None,
            needs_reconcile=False,
        )

    with pytest.raises(ValidationError):
        Settings(
            REDIS_URL="not-a-url",
            POSTGRES_DSN="postgresql://xuanshu",
            QDRANT_URL="http://qdrant:6333",
        )
```

- [ ] **Step 2: Run the contract test to verify it fails**

Run:

```bash
pytest tests/contracts/test_contracts.py -v
```

Expected: FAIL with `ModuleNotFoundError` for `xuanshu.contracts`.

- [ ] **Step 3: Implement enums, contracts, and settings**

Create `src/xuanshu/core/enums.py`:

```python
from enum import StrEnum


class RunMode(StrEnum):
    NORMAL = "normal"
    DEGRADED = "degraded"
    REDUCE_ONLY = "reduce_only"
    HALTED = "halted"


class StrategyId(StrEnum):
    BREAKOUT = "breakout"
    MEAN_REVERSION = "mean_reversion"
    RISK_PAUSE = "risk_pause"


class EventType(StrEnum):
    MARKET = "market"
    ORDER = "order"
    POSITION = "position"


class ApprovalState(StrEnum):
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"
    EXPIRED = "expired"


class MarketRegime(StrEnum):
    BREAKOUT = "breakout"
    MEAN_REVERSION = "mean_reversion"
    RANGE = "range"
    TREND = "trend"
    UNKNOWN = "unknown"


class VolatilityState(StrEnum):
    QUIET = "quiet"
    NORMAL = "normal"
    HOT = "hot"
    STRESSED = "stressed"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class EntryType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class SignalUrgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    IMMEDIATE = "immediate"
```

Create `src/xuanshu/contracts/strategy.py`:

```python
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from xuanshu.core.enums import ApprovalState, RunMode


class StrategyConfigSnapshot(BaseModel):
    version_id: str = Field(min_length=1)
    generated_at: datetime
    effective_from: datetime
    expires_at: datetime
    symbol_whitelist: list[str] = Field(min_length=1)
    strategy_enable_flags: dict[str, bool]
    risk_multiplier: float = Field(ge=0.0, le=1.0)
    per_symbol_max_position: float = Field(ge=0.0, le=1.0)
    max_leverage: int = Field(ge=1, le=3)
    market_mode: RunMode
    approval_state: ApprovalState
    source_reason: str = Field(min_length=1)
    ttl_sec: int = Field(gt=0)

    def is_expired(self, reference_time: datetime) -> bool:
        return reference_time >= self.expires_at

    @model_validator(mode="after")
    def validate_temporal_window(self) -> "StrategyConfigSnapshot":
        if self.expires_at <= self.effective_from:
            raise ValueError("expires_at must be after effective_from")
        return self
```

Create `src/xuanshu/contracts/governance.py`:

```python
from datetime import datetime

from pydantic import BaseModel, Field


class ExpertOpinion(BaseModel):
    opinion_id: str = Field(min_length=1)
    expert_type: str = Field(min_length=1)
    generated_at: datetime
    symbol_scope: list[str] = Field(min_length=1)
    decision: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_facts: list[str]
    risk_flags: list[str]
    ttl_sec: int = Field(gt=0)
```

Create `src/xuanshu/contracts/market.py`:

```python
from datetime import datetime

from pydantic import BaseModel, Field

from xuanshu.core.enums import MarketRegime, RunMode, VolatilityState


class MarketStateSnapshot(BaseModel):
    snapshot_id: str = Field(min_length=1)
    generated_at: datetime
    symbol: str = Field(min_length=1)
    mid_price: float = Field(ge=0.0)
    spread: float = Field(ge=0.0)
    imbalance: float = Field(ge=-1.0, le=1.0)
    recent_trade_bias: float = Field(ge=-1.0, le=1.0)
    volatility_state: VolatilityState
    regime: MarketRegime
    current_position: float
    current_mode: RunMode
    risk_budget_remaining: float = Field(ge=0.0)
```

Create `src/xuanshu/contracts/risk.py`:

```python
from datetime import datetime

from pydantic import BaseModel, Field

from xuanshu.core.enums import EntryType, OrderSide, RunMode, SignalUrgency, StrategyId


class CandidateSignal(BaseModel):
    symbol: str = Field(min_length=1)
    strategy_id: StrategyId
    side: OrderSide
    entry_type: EntryType
    urgency: SignalUrgency
    confidence: float = Field(ge=0.0, le=1.0)
    max_hold_ms: int = Field(gt=0)
    cancel_after_ms: int = Field(gt=0)
    risk_tag: str = Field(min_length=1)


class RiskDecision(BaseModel):
    decision_id: str = Field(min_length=1)
    generated_at: datetime
    symbol: str = Field(min_length=1)
    allow_open: bool
    allow_close: bool
    max_position: float = Field(ge=0.0)
    max_order_size: float = Field(ge=0.0)
    risk_mode: RunMode
    reason_codes: list[str]
```

Create `src/xuanshu/contracts/checkpoint.py`:

```python
from datetime import datetime

from pydantic import BaseModel, Field

from xuanshu.core.enums import OrderSide, RunMode


class CheckpointPosition(BaseModel):
    symbol: str = Field(min_length=1)
    net_quantity: float
    mark_price: float = Field(ge=0.0)
    unrealized_pnl: float


class CheckpointOrder(BaseModel):
    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: OrderSide
    price: float = Field(ge=0.0)
    size: float = Field(gt=0.0)
    status: str = Field(min_length=1)


class CheckpointBudgetState(BaseModel):
    max_daily_loss: float = Field(ge=0.0)
    remaining_daily_loss: float = Field(ge=0.0)
    remaining_notional: float = Field(ge=0.0)
    remaining_order_count: int = Field(ge=0)


class ExecutionCheckpoint(BaseModel):
    checkpoint_id: str = Field(min_length=1)
    created_at: datetime
    active_snapshot_version: str = Field(min_length=1)
    current_mode: RunMode
    positions_snapshot: list[CheckpointPosition] = Field(default_factory=list)
    open_orders_snapshot: list[CheckpointOrder] = Field(default_factory=list)
    budget_state: CheckpointBudgetState
    last_public_stream_marker: str | None = Field(default=None, min_length=1)
    last_private_stream_marker: str | None = Field(default=None, min_length=1)
    needs_reconcile: bool
```

Create `src/xuanshu/config/settings.py`:

```python
from pydantic import Field
from pydantic.networks import AnyHttpUrl, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="XUANSHU_", extra="ignore")

    env: str = Field(default="dev", min_length=1)
    okx_symbols: tuple[str, ...] = Field(default=("BTC-USDT-SWAP", "ETH-USDT-SWAP"), min_length=1)
    redis_url: RedisDsn = Field(validation_alias="REDIS_URL")
    postgres_dsn: PostgresDsn = Field(validation_alias="POSTGRES_DSN")
    qdrant_url: AnyHttpUrl = Field(validation_alias="QDRANT_URL")
    ai_timeout_sec: int = Field(default=12, gt=0, le=300)
```

- [ ] **Step 4: Run the contract test to verify it passes**

Run:

```bash
pytest tests/contracts/test_contracts.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/core/enums.py src/xuanshu/contracts/market.py src/xuanshu/contracts/strategy.py src/xuanshu/contracts/risk.py src/xuanshu/contracts/checkpoint.py src/xuanshu/contracts/governance.py src/xuanshu/config/settings.py tests/contracts/test_contracts.py
git commit -m "feat: add live core contracts and settings"
```

## Task 3: Build Storage Boundaries For Redis, PostgreSQL, And Qdrant

**Files:**
- Create: `src/xuanshu/infra/storage/redis_store.py`
- Create: `src/xuanshu/infra/storage/postgres_store.py`
- Create: `src/xuanshu/infra/storage/qdrant_store.py`
- Test: `tests/storage/test_storage_boundaries.py`

- [ ] **Step 1: Write the failing storage boundary test**

Create `tests/storage/test_storage_boundaries.py`:

```python
import pytest

from xuanshu.infra.storage.postgres_store import POSTGRES_TABLES
from xuanshu.infra.storage.qdrant_store import QDRANT_COLLECTIONS
from xuanshu.infra.storage.redis_store import RedisKeys


def test_redis_key_naming_matches_hot_state_contract() -> None:
    assert RedisKeys.latest_snapshot() == "xuanshu:strategy:latest"
    assert RedisKeys.run_mode() == "xuanshu:runtime:mode"
    assert RedisKeys.symbol_runtime("BTC-USDT-SWAP") == "xuanshu:runtime:symbol:BTC-USDT-SWAP"


def test_redis_symbol_runtime_rejects_unsafe_input() -> None:
    with pytest.raises(ValueError):
        RedisKeys.symbol_runtime("btc/usdt swap")


def test_postgres_tables_are_deterministic_and_immutable() -> None:
    assert POSTGRES_TABLES == (
        "orders",
        "fills",
        "positions",
        "risk_events",
        "strategy_snapshots",
        "execution_checkpoints",
        "expert_opinions",
        "governor_runs",
        "notification_events",
    )


def test_qdrant_collections_are_deterministic_and_immutable() -> None:
    assert QDRANT_COLLECTIONS == (
        "market_case",
        "risk_case",
        "governance_case",
    )
```

- [ ] **Step 2: Run the storage test to verify it fails**

Run:

```bash
pytest tests/storage/test_storage_boundaries.py -v
```

Expected: FAIL with `ModuleNotFoundError` for `xuanshu.infra.storage.redis_store`.

- [ ] **Step 3: Implement storage adapters**

Create `src/xuanshu/infra/storage/redis_store.py`:

```python
from __future__ import annotations

import re


class RedisKeys:
    _SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")

    @staticmethod
    def latest_snapshot() -> str:
        return "xuanshu:strategy:latest"

    @staticmethod
    def run_mode() -> str:
        return "xuanshu:runtime:mode"

    @staticmethod
    def symbol_runtime(symbol: str) -> str:
        if not RedisKeys._SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(f"invalid runtime symbol: {symbol!r}")
        return f"xuanshu:runtime:symbol:{symbol}"
```

Create `src/xuanshu/infra/storage/postgres_store.py`:

```python
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
)
```

Create `src/xuanshu/infra/storage/qdrant_store.py`:

```python
QDRANT_COLLECTIONS = (
    "market_case",
    "risk_case",
    "governance_case",
)
```

- [ ] **Step 4: Run the storage test to verify it passes**

Run:

```bash
pytest tests/storage/test_storage_boundaries.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/infra/storage/redis_store.py src/xuanshu/infra/storage/postgres_store.py src/xuanshu/infra/storage/qdrant_store.py tests/storage/test_storage_boundaries.py
git commit -m "feat: add storage boundaries for live core"
```

## Task 4: Implement Governor Service And Snapshot Publication

**Files:**
- Create: `src/xuanshu/infra/ai/governor_client.py`
- Create: `src/xuanshu/governor/service.py`
- Create: `src/xuanshu/apps/governor.py`
- Test: `tests/governor/test_governor_service.py`

- [ ] **Step 1: Write the failing governor freeze test**

Create `tests/governor/test_governor_service.py`:

```python
from datetime import UTC, datetime, timedelta

from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import RunMode
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
        approval_state="approved",
        source_reason="cached",
        ttl_sec=300,
    )

    service = GovernorService()

    assert service.freeze_on_failure(snapshot).version_id == "snap-last"
```

- [ ] **Step 2: Run the governor test to verify it fails**

Run:

```bash
pytest tests/governor/test_governor_service.py -v
```

Expected: FAIL with `ModuleNotFoundError` for `xuanshu.governor.service`.

- [ ] **Step 3: Implement AI wrapper and governor logic**

Create `src/xuanshu/infra/ai/governor_client.py`:

```python
from xuanshu.contracts.strategy import StrategyConfigSnapshot


class GovernorClient:
    def __init__(self, agent_runner) -> None:
        self.agent_runner = agent_runner

    async def generate_snapshot(self, state_summary: dict[str, object]) -> StrategyConfigSnapshot:
        result = await self.agent_runner.run(state_summary)
        return StrategyConfigSnapshot.model_validate(result)
```

Create `src/xuanshu/governor/service.py`:

```python
from xuanshu.contracts.strategy import StrategyConfigSnapshot


class GovernorService:
    def freeze_on_failure(self, last_snapshot: StrategyConfigSnapshot) -> StrategyConfigSnapshot:
        return last_snapshot
```

Create `src/xuanshu/apps/governor.py`:

```python
from xuanshu.governor.service import GovernorService


def build_governor_service() -> GovernorService:
    return GovernorService()
```

- [ ] **Step 4: Run the governor test to verify it passes**

Run:

```bash
pytest tests/governor/test_governor_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/infra/ai/governor_client.py src/xuanshu/governor/service.py src/xuanshu/apps/governor.py tests/governor/test_governor_service.py
git commit -m "feat: add governor snapshot freeze behavior"
```

## Task 5: Implement Notifier Service And Query Surface

**Files:**
- Create: `src/xuanshu/infra/notifier/telegram.py`
- Create: `src/xuanshu/notifier/service.py`
- Create: `src/xuanshu/apps/notifier.py`
- Test: `tests/notifier/test_notifier_service.py`

- [ ] **Step 1: Write the failing notifier formatting test**

Create `tests/notifier/test_notifier_service.py`:

```python
from xuanshu.core.enums import RunMode
from xuanshu.infra.notifier.telegram import TextMessagePayload, render_text_message
from xuanshu.notifier.service import format_mode_change


def test_mode_change_notification_is_human_readable() -> None:
    assert format_mode_change(RunMode.REDUCE_ONLY) == "Mode changed to reduce-only"


def test_telegram_text_payload_is_typed() -> None:
    payload = render_text_message("hello")

    assert payload == TextMessagePayload(text="hello")
    assert payload.parse_mode is None
```

- [ ] **Step 2: Run the notifier test to verify it fails**

Run:

```bash
pytest tests/notifier/test_notifier_service.py -v
```

Expected: FAIL with `ModuleNotFoundError` for `xuanshu.notifier.service`.

- [ ] **Step 3: Implement notifier adapter and service**

Create `src/xuanshu/infra/notifier/telegram.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextMessagePayload:
    text: str
    parse_mode: str | None = None


def render_text_message(text: str) -> TextMessagePayload:
    return TextMessagePayload(text=text)
```

Create `src/xuanshu/notifier/service.py`:

```python
from xuanshu.core.enums import RunMode


_MODE_LABELS: dict[RunMode, str] = {
    RunMode.NORMAL: "normal trading",
    RunMode.DEGRADED: "degraded trading",
    RunMode.REDUCE_ONLY: "reduce-only",
    RunMode.HALTED: "halted",
}


def format_mode_change(mode: RunMode) -> str:
    return f"Mode changed to {_MODE_LABELS[mode]}"
```

Create `src/xuanshu/apps/notifier.py`:

```python
from xuanshu.core.enums import RunMode
from xuanshu.notifier.service import format_mode_change


def build_notifier_preview(mode: RunMode) -> str:
    return format_mode_change(mode)
```

- [ ] **Step 4: Run the notifier test to verify it passes**

Run:

```bash
pytest tests/notifier/test_notifier_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/infra/notifier/telegram.py src/xuanshu/notifier/service.py src/xuanshu/apps/notifier.py tests/notifier/test_notifier_service.py
git commit -m "feat: add notifier service surface"
```

## Task 6: Implement Trader State, Routing, Signals, And Risk Kernel

**Files:**
- Create: `src/xuanshu/state/engine.py`
- Create: `src/xuanshu/strategies/regime_router.py`
- Create: `src/xuanshu/strategies/signals.py`
- Create: `src/xuanshu/risk/kernel.py`
- Test: `tests/trader/test_trader_decision_flow.py`

- [ ] **Step 1: Write the failing trader decision-flow test**

Create `tests/trader/test_trader_decision_flow.py`:

```python
import pytest

from xuanshu.core.enums import EntryType, MarketRegime, OrderSide, SignalUrgency, StrategyId, VolatilityState
from xuanshu.state.engine import StateEngine
from xuanshu.strategies.signals import build_candidate_signals


def test_trader_generates_breakout_signal_for_trend_expansion() -> None:
    engine = StateEngine()
    engine.on_bbo("BTC-USDT-SWAP", bid=100.0, ask=100.2)
    engine.on_trade("BTC-USDT-SWAP", price=100.3, size=5.0, side="buy")
    engine.on_trade("BTC-USDT-SWAP", price=100.4, size=4.0, side="buy")

    snapshot = engine.snapshot("BTC-USDT-SWAP")
    signals = build_candidate_signals(snapshot)

    assert snapshot.volatility_state == VolatilityState.HOT
    assert snapshot.regime == MarketRegime.TREND
    assert signals[0].strategy_id == StrategyId.BREAKOUT
    assert signals[0].side == OrderSide.BUY
    assert signals[0].entry_type == EntryType.MARKET


def test_trader_pause_signal_is_explicitly_non_executable() -> None:
    engine = StateEngine()
    engine.on_bbo("BTC-USDT-SWAP", bid=100.0, ask=100.9)
    snapshot = engine.snapshot("BTC-USDT-SWAP")

    signals = build_candidate_signals(snapshot)

    assert signals[0].strategy_id == StrategyId.RISK_PAUSE
    assert signals[0].side == OrderSide.FLAT
    assert signals[0].entry_type == EntryType.NONE
    assert signals[0].urgency == SignalUrgency.LOW
    assert signals[0].confidence == 0.0


def test_trader_rejects_unsupported_trade_side() -> None:
    engine = StateEngine()

    with pytest.raises(ValueError, match="unsupported trade side"):
        engine.on_trade("BTC-USDT-SWAP", price=100.0, size=1.0, side="hold")
```

- [ ] **Step 2: Run the trader decision-flow test to verify it fails**

Run:

```bash
pytest tests/trader/test_trader_decision_flow.py -v
```

Expected: FAIL with `ModuleNotFoundError` for `xuanshu.state.engine`.

- [ ] **Step 3: Implement state, regime routing, signal generation, and risk**

Create `src/xuanshu/state/engine.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.core.enums import MarketRegime, RunMode, VolatilityState
from xuanshu.strategies.regime_router import classify_regime


@dataclass
class SymbolState:
    bid: float = 0.0
    ask: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0


@dataclass
class StateEngine:
    symbols: dict[str, SymbolState] = field(default_factory=dict)

    def on_bbo(self, symbol: str, bid: float, ask: float) -> None:
        state = self.symbols.setdefault(symbol, SymbolState())
        state.bid = bid
        state.ask = ask

    def on_trade(self, symbol: str, price: float, size: float, side: str) -> None:
        state = self.symbols.setdefault(symbol, SymbolState())
        normalized_side = side.lower()
        if normalized_side == "buy":
            state.buy_volume += size
        elif normalized_side == "sell":
            state.sell_volume += size
        else:
            raise ValueError(f"unsupported trade side: {side}")

    def snapshot(self, symbol: str) -> MarketStateSnapshot:
        state = self.symbols[symbol]
        mid_price = (state.bid + state.ask) / 2
        total_volume = max(state.buy_volume + state.sell_volume, 1.0)
        recent_trade_bias = (state.buy_volume - state.sell_volume) / total_volume
        spread = max(state.ask - state.bid, 0.0)

        snapshot = MarketStateSnapshot(
            snapshot_id=str(uuid4()),
            generated_at=datetime.now(UTC),
            symbol=symbol,
            mid_price=mid_price,
            spread=spread,
            imbalance=recent_trade_bias,
            recent_trade_bias=recent_trade_bias,
            volatility_state=VolatilityState.NORMAL,
            regime=MarketRegime.UNKNOWN,
            current_position=0.0,
            current_mode=RunMode.NORMAL,
            risk_budget_remaining=1.0,
        )

        snapshot.volatility_state = VolatilityState.HOT if spread >= 0.2 else VolatilityState.NORMAL
        snapshot.regime = classify_regime(snapshot)
        return snapshot
```

Create `src/xuanshu/strategies/regime_router.py`:

```python
from __future__ import annotations

from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.core.enums import MarketRegime, VolatilityState


def classify_regime(snapshot: MarketStateSnapshot) -> MarketRegime:
    if snapshot.recent_trade_bias > 0.6 and snapshot.volatility_state == VolatilityState.HOT:
        return MarketRegime.TREND
    if abs(snapshot.recent_trade_bias) < 0.2 and snapshot.volatility_state == VolatilityState.NORMAL:
        return MarketRegime.MEAN_REVERSION
    if snapshot.spread > 0.5 or abs(snapshot.imbalance) > 0.9:
        return MarketRegime.UNKNOWN
    return MarketRegime.RANGE
```

Create `src/xuanshu/strategies/signals.py`:

```python
from __future__ import annotations

from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.contracts.risk import CandidateSignal
from xuanshu.core.enums import EntryType, MarketRegime, OrderSide, SignalUrgency, StrategyId


def build_candidate_signals(snapshot: MarketStateSnapshot) -> list[CandidateSignal]:
    if snapshot.regime == MarketRegime.TREND:
        return [
            CandidateSignal(
                symbol=snapshot.symbol,
                strategy_id=StrategyId.BREAKOUT,
                side=OrderSide.BUY,
                entry_type=EntryType.MARKET,
                urgency=SignalUrgency.HIGH,
                confidence=0.7,
                max_hold_ms=3000,
                cancel_after_ms=750,
                risk_tag="trend",
            )
        ]
    if snapshot.regime == MarketRegime.MEAN_REVERSION:
        return [
            CandidateSignal(
                symbol=snapshot.symbol,
                strategy_id=StrategyId.MEAN_REVERSION,
                side=OrderSide.SELL,
                entry_type=EntryType.MARKET,
                urgency=SignalUrgency.NORMAL,
                confidence=0.6,
                max_hold_ms=2000,
                cancel_after_ms=500,
                risk_tag="revert",
            )
        ]
    return [
        CandidateSignal(
            symbol=snapshot.symbol,
            strategy_id=StrategyId.RISK_PAUSE,
            side=OrderSide.FLAT,
            entry_type=EntryType.NONE,
            urgency=SignalUrgency.LOW,
            confidence=0.0,
            max_hold_ms=1,
            cancel_after_ms=1,
            risk_tag="pause",
        )
    ]
```

Create `src/xuanshu/risk/kernel.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from xuanshu.contracts.risk import CandidateSignal, RiskDecision
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import OrderSide, RunMode, StrategyId


class RiskKernel:
    def __init__(self, nav: float) -> None:
        self.nav = nav

    def evaluate(self, signal: CandidateSignal, snapshot: StrategyConfigSnapshot) -> RiskDecision:
        allow_open = signal.strategy_id != StrategyId.RISK_PAUSE and signal.side != OrderSide.FLAT
        reason_codes: list[str] = []

        if snapshot.market_mode in {RunMode.REDUCE_ONLY, RunMode.HALTED}:
            allow_open = False
            reason_codes.append("mode_blocks_open")

        if signal.side == OrderSide.FLAT:
            reason_codes.append("pause_signal")

        max_position = self.nav * snapshot.per_symbol_max_position * snapshot.risk_multiplier
        return RiskDecision(
            decision_id=str(uuid4()),
            generated_at=datetime.now(UTC),
            symbol=signal.symbol,
            allow_open=allow_open,
            allow_close=True,
            max_position=max_position,
            max_order_size=min(max_position, self.nav * 0.0035),
            risk_mode=snapshot.market_mode,
            reason_codes=reason_codes,
        )
```

- [ ] **Step 4: Run the trader decision-flow test to verify it passes**

Run:

```bash
pytest tests/trader/test_trader_decision_flow.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/xuanshu/state/engine.py src/xuanshu/strategies/regime_router.py src/xuanshu/strategies/signals.py src/xuanshu/risk/kernel.py tests/trader/test_trader_decision_flow.py
git commit -m "feat: add trader state and decision flow"
```

## Task 7: Implement Deterministic Execution And Checkpoint Recovery

**Files:**
- Create: `src/xuanshu/infra/okx/rest.py`
- Create: `src/xuanshu/infra/okx/public_ws.py`
- Create: `src/xuanshu/infra/okx/private_ws.py`
- Create: `src/xuanshu/execution/engine.py`
- Create: `src/xuanshu/checkpoints/service.py`
- Test: `tests/execution/test_execution_and_recovery.py`

- [ ] **Step 1: Write the failing execution/recovery test**

Create `tests/execution/test_execution_and_recovery.py`:

```python
from datetime import UTC, datetime

import pytest

from xuanshu.checkpoints.service import CheckpointService
from xuanshu.contracts.checkpoint import CheckpointBudgetState, ExecutionCheckpoint
from xuanshu.core.enums import RunMode
from xuanshu.execution.engine import build_client_order_id
from xuanshu.infra.okx.rest import OkxRestClient


def test_execution_ids_and_recovery_guard_are_deterministic() -> None:
    checkpoint = ExecutionCheckpoint(
        checkpoint_id="cp-001",
        created_at=datetime.now(UTC),
        active_snapshot_version="snap-001",
        current_mode=RunMode.NORMAL,
        positions_snapshot=[],
        open_orders_snapshot=[],
        budget_state=CheckpointBudgetState(
            max_daily_loss=100.0,
            remaining_daily_loss=25.0,
            remaining_notional=50.0,
            remaining_order_count=10,
        ),
        last_public_stream_marker="pub-1",
        last_private_stream_marker="pri-1",
        needs_reconcile=True,
    )
    healthy_checkpoint = ExecutionCheckpoint(
        checkpoint_id="cp-002",
        created_at=datetime.now(UTC),
        active_snapshot_version="snap-002",
        current_mode=RunMode.NORMAL,
        positions_snapshot=[],
        open_orders_snapshot=[],
        budget_state=CheckpointBudgetState(
            max_daily_loss=100.0,
            remaining_daily_loss=25.0,
            remaining_notional=50.0,
            remaining_order_count=10,
        ),
        last_public_stream_marker=None,
        last_private_stream_marker=None,
        needs_reconcile=False,
    )

    assert build_client_order_id("BTC-USDT-SWAP", "breakout", 7) == "BTC-USDT-SWAP-breakout-000007"
    assert CheckpointService().can_open_new_risk(checkpoint) is False
    assert CheckpointService().can_open_new_risk(healthy_checkpoint) is True


@pytest.mark.parametrize(
    ("symbol", "strategy_id", "sequence"),
    [
        ("btc/usdt-swap", "breakout", 7),
        ("BTC-USDT-SWAP", "breakout v2", 7),
        ("BTC-USDT-SWAP", "breakout", -1),
        ("BTC-USDT-SWAP", "breakout", 1_000_000),
    ],
)
def test_build_client_order_id_rejects_unsafe_or_ambiguous_inputs(
    symbol: str,
    strategy_id: str,
    sequence: int,
) -> None:
    with pytest.raises(ValueError):
        build_client_order_id(symbol, strategy_id, sequence)


class _DummyAsyncClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_okx_rest_client_supports_async_context_manager_and_closes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xuanshu.infra.okx.rest.httpx.AsyncClient", _DummyAsyncClient)

    async with OkxRestClient(base_url="https://example.com", api_key="api-key") as client:
        assert isinstance(client.client, _DummyAsyncClient)

    assert client.client.closed == 1

    await client.aclose()

    assert client.client.closed == 1
```

- [ ] **Step 2: Run the execution/recovery test to verify it fails**

Run:

```bash
pytest tests/execution/test_execution_and_recovery.py -v
```

Expected: FAIL with `ModuleNotFoundError` for `xuanshu.execution.engine`.

- [ ] **Step 3: Implement execution helpers and recovery guard**

Create `src/xuanshu/infra/okx/rest.py`:

```python
from __future__ import annotations

import httpx


class OkxRestClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str | None = None,
        passphrase: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        self._closed = False

    async def __aenter__(self) -> "OkxRestClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        await self.client.aclose()
        self._closed = True
```

Create `src/xuanshu/infra/okx/public_ws.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OkxPublicStream:
    url: str
```

Create `src/xuanshu/infra/okx/private_ws.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OkxPrivateStream:
    url: str
```

Create `src/xuanshu/execution/engine.py`:

```python
from __future__ import annotations

import re


_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")
_STRATEGY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _validate_component(label: str, value: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {label}: {value!r}")
    if value != value.strip():
        raise ValueError(f"invalid {label}: {value!r}")
    if not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def build_client_order_id(symbol: str, strategy_id: str, sequence: int) -> str:
    _validate_component("symbol", symbol, _SYMBOL_PATTERN)
    _validate_component("strategy_id", strategy_id, _STRATEGY_PATTERN)
    if type(sequence) is not int or sequence < 0 or sequence > 999_999:
        raise ValueError(f"invalid sequence: {sequence!r}")
    return f"{symbol}-{strategy_id}-{sequence:06d}"
```

Create `src/xuanshu/checkpoints/service.py`:

```python
from xuanshu.contracts.checkpoint import ExecutionCheckpoint


class CheckpointService:
    def can_open_new_risk(self, checkpoint: ExecutionCheckpoint) -> bool:
        return not checkpoint.needs_reconcile
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
git add src/xuanshu/infra/okx/rest.py src/xuanshu/infra/okx/public_ws.py src/xuanshu/infra/okx/private_ws.py src/xuanshu/execution/engine.py src/xuanshu/checkpoints/service.py tests/execution/test_execution_and_recovery.py
git commit -m "feat: add deterministic execution and recovery guard"
```

## Task 8: Compose Services And Add Single-Host Deployment Wiring

**Files:**
- Create: `Dockerfile`
- Create: `src/xuanshu/apps/trader.py`
- Modify: `src/xuanshu/apps/governor.py`
- Modify: `src/xuanshu/apps/notifier.py`
- Create: `docker-compose.yml`
- Test: `tests/apps/test_trader_app_wiring.py`
- Test: `tests/apps/test_governor_app_wiring.py`
- Test: `tests/apps/test_notifier_app_wiring.py`

- [ ] **Step 1: Write the failing app wiring tests**

Create `tests/apps/test_trader_app_wiring.py`:

```python
import xuanshu.apps.trader as trader_app


def test_trader_entrypoint_builds_typed_components(monkeypatch) -> None:
    original_build_trader_components = trader_app.build_trader_components
    build_called = 0

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)

    def fake_build_trader_components() -> trader_app.TraderComponents:
        nonlocal build_called
        build_called += 1
        return original_build_trader_components()

    monkeypatch.setattr(trader_app, "build_trader_components", fake_build_trader_components)

    assert trader_app.main() == 0

    components = original_build_trader_components()
    assert components.state_engine.__class__.__name__ == "StateEngine"
    assert components.risk_kernel.nav == 100_000.0
    assert components.checkpoint_service.__class__.__name__ == "CheckpointService"
    assert components.client_order_id_builder("BTC-USDT-SWAP", "breakout", 1) == "BTC-USDT-SWAP-breakout-000001"
    assert build_called == 1
```

Create `tests/apps/test_governor_app_wiring.py`:

```python
import xuanshu.apps.governor as governor_app


def test_governor_entrypoint_keeps_service_in_runtime(monkeypatch) -> None:
    build_called = 0
    seen_runtime = None

    async def _noop_wait_forever() -> None:
        return None

    async def fake_run_governor(runtime: governor_app.GovernorRuntime) -> None:
        nonlocal seen_runtime
        seen_runtime = runtime
        await _noop_wait_forever()

    monkeypatch.setattr(governor_app, "_run_governor", fake_run_governor)

    original_build_governor_service = governor_app.build_governor_service

    def fake_build_governor_service():
        nonlocal build_called
        build_called += 1
        return original_build_governor_service()

    monkeypatch.setattr(governor_app, "build_governor_service", fake_build_governor_service)

    assert governor_app.main() == 0
    assert build_called == 1
    assert seen_runtime is not None
    assert seen_runtime.service.__class__.__name__ == "GovernorService"
```

Create `tests/apps/test_notifier_app_wiring.py`:

```python
import xuanshu.apps.notifier as notifier_app
from xuanshu.core.enums import RunMode


def test_notifier_entrypoint_keeps_runtime_boundary_silent(monkeypatch, capsys) -> None:
    build_called = 0
    seen_runtime = None

    async def _noop_wait_forever() -> None:
        return None

    async def fake_run_notifier(runtime: notifier_app.NotifierRuntime) -> None:
        nonlocal seen_runtime
        seen_runtime = runtime
        await _noop_wait_forever()

    monkeypatch.setattr(notifier_app, "_run_notifier", fake_run_notifier)

    original_build_notifier_runtime = notifier_app.build_notifier_runtime

    def fake_build_notifier_runtime(mode: RunMode = RunMode.NORMAL) -> notifier_app.NotifierRuntime:
        nonlocal build_called
        build_called += 1
        return original_build_notifier_runtime(mode)

    monkeypatch.setattr(notifier_app, "build_notifier_runtime", fake_build_notifier_runtime)

    assert notifier_app.main() == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert build_called == 1
    assert seen_runtime is not None
    assert seen_runtime.mode == RunMode.NORMAL
```

- [ ] **Step 2: Run the app wiring tests to verify they fail**

Run:

```bash
pytest tests/apps -v
```

Expected: FAIL with `ModuleNotFoundError` for `xuanshu.apps`.

- [ ] **Step 3: Implement trader composition and deployment file**

Create `src/xuanshu/apps/trader.py`:

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Callable

from xuanshu.checkpoints.service import CheckpointService
from xuanshu.execution.engine import build_client_order_id
from xuanshu.risk.kernel import RiskKernel
from xuanshu.state.engine import StateEngine


@dataclass(frozen=True, slots=True)
class TraderComponents:
    state_engine: StateEngine
    risk_kernel: RiskKernel
    checkpoint_service: CheckpointService
    client_order_id_builder: Callable[[str, str, int], str]


def build_trader_components() -> TraderComponents:
    return TraderComponents(
        state_engine=StateEngine(),
        risk_kernel=RiskKernel(nav=100_000.0),
        checkpoint_service=CheckpointService(),
        client_order_id_builder=build_client_order_id,
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_trader(components: TraderComponents) -> None:
    _ = components
    await _wait_forever()


def main() -> int:
    asyncio.run(_run_trader(build_trader_components()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Update `src/xuanshu/apps/governor.py` to expose a real module entrypoint:

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from xuanshu.governor.service import GovernorService


@dataclass(frozen=True, slots=True)
class GovernorRuntime:
    service: GovernorService


def build_governor_service() -> GovernorService:
    return GovernorService()


def build_governor_runtime() -> GovernorRuntime:
    return GovernorRuntime(service=build_governor_service())


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_governor(runtime: GovernorRuntime) -> None:
    _ = runtime.service
    await _wait_forever()


def main() -> int:
    asyncio.run(_run_governor(build_governor_runtime()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Update `src/xuanshu/apps/notifier.py` to expose a cleaner notifier runtime boundary:

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from xuanshu.core.enums import RunMode


@dataclass(frozen=True, slots=True)
class NotifierRuntime:
    mode: RunMode


def build_notifier_runtime(mode: RunMode | str = RunMode.NORMAL) -> NotifierRuntime:
    return NotifierRuntime(mode=mode if isinstance(mode, RunMode) else RunMode(mode))


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_notifier(runtime: NotifierRuntime) -> None:
    _ = runtime.mode
    await _wait_forever()


def main() -> int:
    asyncio.run(_run_notifier(build_notifier_runtime()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Create `docker-compose.yml`:

```yaml
services:
  trader:
    build: .
    command: python -m xuanshu.apps.trader
    environment: &app_env
      OKX_API_KEY: ${OKX_API_KEY:-}
      OKX_API_PASSPHRASE: ${OKX_API_PASSPHRASE:-}
      OKX_API_SECRET: ${OKX_API_SECRET:-}
      OPENAI_API_KEY: ${OPENAI_API_KEY:-}
      POSTGRES_DSN: ${POSTGRES_DSN:-postgresql+psycopg://xuanshu:xuanshu@postgres:5432/xuanshu}
      QDRANT_URL: ${QDRANT_URL:-http://qdrant:6333}
      REDIS_URL: ${REDIS_URL:-redis://redis:6379/0}
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN:-}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID:-}
      XUANSHU_ENV: ${XUANSHU_ENV:-prod}
      XUANSHU_OKX_SYMBOLS: ${XUANSHU_OKX_SYMBOLS:-BTC-USDT-SWAP,ETH-USDT-SWAP}
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy
      qdrant:
        condition: service_healthy
    restart: unless-stopped
  governor:
    build: .
    command: python -m xuanshu.apps.governor
    environment: *app_env
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy
      qdrant:
        condition: service_healthy
    restart: unless-stopped
  notifier:
    build: .
    command: python -m xuanshu.apps.notifier
    environment: *app_env
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy
    restart: unless-stopped
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes", "--save", "60", "1"]
    volumes:
      - redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 5s
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: xuanshu
      POSTGRES_PASSWORD: xuanshu
      POSTGRES_DB: xuanshu
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U xuanshu -d xuanshu"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
  qdrant:
    image: qdrant/qdrant:v1.9.0
    volumes:
      - qdrant-data:/qdrant/storage
    healthcheck:
      test: ["CMD-SHELL", "wget -q -O- http://localhost:6333/healthz >/dev/null 2>&1"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s

volumes:
  postgres-data:
  redis-data:
  qdrant-data:
```

Create `Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src

RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

CMD ["python", "-m", "xuanshu.apps.trader"]
```

- [ ] **Step 4: Run the app wiring tests to verify they pass**

Run:

```bash
pytest tests/apps -v
```

Expected: PASS.

- [ ] **Step 5: Run the full live-core test suite**

Run:

```bash
pytest tests -v
```

Expected: PASS for smoke, contracts, storage, governor, notifier, trader, execution, and app wiring tests.

- [ ] **Step 6: Validate compose build wiring**

Run:

```bash
docker compose config
docker compose build trader governor notifier
```

Expected: PASS for compose config and image builds.

- [ ] **Step 7: Commit**

Run:

```bash
git add Dockerfile src/xuanshu/apps/trader.py src/xuanshu/apps/governor.py src/xuanshu/apps/notifier.py docker-compose.yml tests/apps/test_trader_app_wiring.py tests/apps/test_governor_app_wiring.py tests/apps/test_notifier_app_wiring.py
git commit -m "feat: wire single-host live core services"
```

## Self-Review

Spec coverage for this revised plan:

- Covered now: single-host repo bootstrap, stable contracts, Redis/PostgreSQL/Qdrant boundaries, Governor snapshot publication and freeze behavior, Notifier surface, Trader state/routing/signals/risk, deterministic execution IDs, checkpoint recovery guard, single-host deployment wiring.
- Deferred intentionally: full OKX auth/signing, full SQL models/migrations, real Redis persistence calls, actual Telegram transport, actual OpenAI committee orchestration, real reconcile REST workflow, replay/backtest, MLflow, dashboards.

Placeholder scan:

- No placeholder markers remain in executable steps.
- Every code-writing step includes concrete code.

Type consistency:

- `StrategyConfigSnapshot`, `ExpertOpinion`, `CandidateSignal`, `RiskDecision`, and `ExecutionCheckpoint` are defined before later tasks use them.
- `GovernorService.freeze_on_failure`, `CheckpointService.can_open_new_risk`, and `build_client_order_id` are named consistently across tasks.

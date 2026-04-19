# Trader Live Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `Trader Service` into a production-shaped live execution loop with real OKX websocket inputs, real REST execution, startup recovery, Redis hot-state publication, and PostgreSQL execution fact persistence.

**Architecture:** Extend the current trader skeleton by introducing explicit trader events, a dispatcher, an execution coordinator, and a recovery supervisor. Keep `ExecutionEngine` pure, keep `StateEngine` as in-memory truth, and move all network side effects into OKX adapters and coordinator modules so the trader app becomes a composition root plus runtime loop.

**Tech Stack:** Python 3.13, asyncio, pydantic, pydantic-settings, httpx, websockets, redis-py, pytest, pytest-asyncio

---

## File Structure

- Modify: `src/xuanshu/core/enums.py`
  Add event types needed by the live trader runtime without overloading the existing coarse `MARKET / ORDER / POSITION` set.
- Create: `src/xuanshu/contracts/events.py`
  Define the normalized public/private/fault/runtime event payloads shared by OKX adapters, dispatcher, coordinator, and recovery.
- Modify: `src/xuanshu/infra/okx/public_ws.py`
  Implement subscription payload building and decoding of market events into normalized trader events.
- Modify: `src/xuanshu/infra/okx/private_ws.py`
  Implement login payload building and decoding of order, position, account, and fault events.
- Modify: `src/xuanshu/infra/okx/rest.py`
  Add signed request helpers plus minimal live methods for place order, list open orders, list positions, and account balance summary.
- Modify: `src/xuanshu/state/engine.py`
  Expand from quote/trade-only memory into trader runtime truth with positions, open orders, run mode, fault flags, stream markers, and runtime summaries.
- Modify: `src/xuanshu/infra/storage/redis_store.py`
  Add symbol runtime summary/fault summary write paths that the trader can publish.
- Modify: `src/xuanshu/infra/storage/postgres_store.py`
  Replace constant-only placeholder with append-oriented persistence methods for orders, fills, positions, risk events, and checkpoints.
- Modify: `src/xuanshu/execution/engine.py`
  Keep pure behavior but add execution intent and OKX payload construction.
- Create: `src/xuanshu/execution/coordinator.py`
  Orchestrate live order placement/cancel behavior, in-flight intent tracking, and feedback correlation.
- Create: `src/xuanshu/trader/dispatcher.py`
  Route normalized events into state updates, execution feedback, and recovery escalation.
- Create: `src/xuanshu/trader/recovery.py`
  Implement startup recovery and reconciliation decisions.
- Modify: `src/xuanshu/apps/trader.py`
  Replace the startup-only wait loop with the real runtime wiring and event loop.
- Create/Modify tests:
  - `tests/contracts/test_event_contracts.py`
  - `tests/storage/test_storage_boundaries.py`
  - `tests/execution/test_okx_execution_engine.py`
  - `tests/execution/test_execution_coordinator.py`
  - `tests/trader/test_dispatcher.py`
  - `tests/trader/test_recovery.py`
  - `tests/apps/test_trader_app_wiring.py`

## Task 1: Add Normalized Trader Event Contracts

**Files:**
- Modify: `src/xuanshu/core/enums.py`
- Create: `src/xuanshu/contracts/events.py`
- Test: `tests/contracts/test_event_contracts.py`

- [ ] **Step 1: Write the failing event-contract tests**

Create `tests/contracts/test_event_contracts.py`:

```python
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from xuanshu.contracts.events import (
    AccountSnapshotEvent,
    FaultEvent,
    MarketTradeEvent,
    OrderUpdateEvent,
    OrderbookTopEvent,
    PositionUpdateEvent,
)
from xuanshu.core.enums import TraderEventType


def test_trader_event_contracts_are_stable_and_typed() -> None:
    generated_at = datetime.now(UTC)

    orderbook = OrderbookTopEvent(
        event_type=TraderEventType.ORDERBOOK_TOP,
        symbol="BTC-USDT-SWAP",
        exchange="okx",
        generated_at=generated_at,
        public_sequence="pub-1",
        bid_price=100.0,
        ask_price=100.1,
        bid_size=5.0,
        ask_size=6.0,
    )
    trade = MarketTradeEvent(
        event_type=TraderEventType.MARKET_TRADE,
        symbol="BTC-USDT-SWAP",
        exchange="okx",
        generated_at=generated_at,
        public_sequence="pub-2",
        price=100.2,
        size=1.5,
        side="buy",
    )
    order = OrderUpdateEvent(
        event_type=TraderEventType.ORDER_UPDATE,
        symbol="BTC-USDT-SWAP",
        exchange="okx",
        generated_at=generated_at,
        private_sequence="pri-1",
        order_id="123",
        client_order_id="btc-breakout-000001",
        side="buy",
        price=100.2,
        size=1.0,
        filled_size=0.0,
        status="live",
    )
    position = PositionUpdateEvent(
        event_type=TraderEventType.POSITION_UPDATE,
        symbol="BTC-USDT-SWAP",
        exchange="okx",
        generated_at=generated_at,
        private_sequence="pri-2",
        net_quantity=1.0,
        average_price=100.2,
        mark_price=100.4,
        unrealized_pnl=0.2,
    )
    account = AccountSnapshotEvent(
        event_type=TraderEventType.ACCOUNT_SNAPSHOT,
        exchange="okx",
        generated_at=generated_at,
        private_sequence="pri-3",
        equity=1000.0,
        available_balance=800.0,
        margin_ratio=0.15,
    )
    fault = FaultEvent(
        event_type=TraderEventType.RUNTIME_FAULT,
        exchange="okx",
        generated_at=generated_at,
        severity="warn",
        code="public_ws_disconnected",
        detail="public stream dropped",
    )

    assert orderbook.event_type == TraderEventType.ORDERBOOK_TOP
    assert trade.side == "buy"
    assert order.status == "live"
    assert position.net_quantity == 1.0
    assert account.available_balance == 800.0
    assert fault.code == "public_ws_disconnected"


def test_trader_event_contracts_reject_invalid_sequences_and_prices() -> None:
    generated_at = datetime.now(UTC)

    with pytest.raises(ValidationError):
        OrderbookTopEvent(
            event_type=TraderEventType.ORDERBOOK_TOP,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=generated_at,
            public_sequence="",
            bid_price=100.0,
            ask_price=99.9,
            bid_size=5.0,
            ask_size=6.0,
        )

    with pytest.raises(ValidationError):
        PositionUpdateEvent(
            event_type=TraderEventType.POSITION_UPDATE,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=generated_at,
            private_sequence="pri-2",
            net_quantity=1.0,
            average_price=-1.0,
            mark_price=100.4,
            unrealized_pnl=0.2,
        )
```

- [ ] **Step 2: Run the event-contract tests to verify they fail**

Run:

```bash
./.venv/bin/python -m pytest tests/contracts/test_event_contracts.py -v
```

Expected: FAIL with import errors for `xuanshu.contracts.events` or missing `TraderEventType`.

- [ ] **Step 3: Implement the new enum values**

Update `src/xuanshu/core/enums.py`:

```python
class TraderEventType(StrEnum):
    ORDERBOOK_TOP = "orderbook_top"
    MARKET_TRADE = "market_trade"
    ORDER_UPDATE = "order_update"
    POSITION_UPDATE = "position_update"
    ACCOUNT_SNAPSHOT = "account_snapshot"
    RUNTIME_FAULT = "runtime_fault"
```

- [ ] **Step 4: Implement normalized trader event contracts**

Create `src/xuanshu/contracts/events.py`:

```python
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from xuanshu.core.enums import TraderEventType


class _TraderEvent(BaseModel):
    event_type: TraderEventType
    exchange: str = Field(min_length=1)
    generated_at: datetime

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(UTC)


class OrderbookTopEvent(_TraderEvent):
    event_type: Literal[TraderEventType.ORDERBOOK_TOP]
    symbol: str = Field(min_length=1)
    public_sequence: str = Field(min_length=1)
    bid_price: float = Field(ge=0.0)
    ask_price: float = Field(ge=0.0)
    bid_size: float = Field(ge=0.0)
    ask_size: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_prices(self) -> "OrderbookTopEvent":
        if self.ask_price < self.bid_price:
            raise ValueError("ask_price must be >= bid_price")
        return self


class MarketTradeEvent(_TraderEvent):
    event_type: Literal[TraderEventType.MARKET_TRADE]
    symbol: str = Field(min_length=1)
    public_sequence: str = Field(min_length=1)
    price: float = Field(ge=0.0)
    size: float = Field(gt=0.0)
    side: Literal["buy", "sell"]


class OrderUpdateEvent(_TraderEvent):
    event_type: Literal[TraderEventType.ORDER_UPDATE]
    symbol: str = Field(min_length=1)
    private_sequence: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    client_order_id: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    price: float = Field(ge=0.0)
    size: float = Field(gt=0.0)
    filled_size: float = Field(ge=0.0)
    status: str = Field(min_length=1)


class PositionUpdateEvent(_TraderEvent):
    event_type: Literal[TraderEventType.POSITION_UPDATE]
    symbol: str = Field(min_length=1)
    private_sequence: str = Field(min_length=1)
    net_quantity: float
    average_price: float = Field(ge=0.0)
    mark_price: float = Field(ge=0.0)
    unrealized_pnl: float


class AccountSnapshotEvent(_TraderEvent):
    event_type: Literal[TraderEventType.ACCOUNT_SNAPSHOT]
    private_sequence: str = Field(min_length=1)
    equity: float = Field(ge=0.0)
    available_balance: float = Field(ge=0.0)
    margin_ratio: float = Field(ge=0.0)


class FaultEvent(_TraderEvent):
    event_type: Literal[TraderEventType.RUNTIME_FAULT]
    severity: Literal["info", "warn", "critical"]
    code: str = Field(min_length=1)
    detail: str = Field(min_length=1)
```

- [ ] **Step 5: Run the event-contract tests to verify they pass**

Run:

```bash
./.venv/bin/python -m pytest tests/contracts/test_event_contracts.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit the event contracts**

Run:

```bash
git add src/xuanshu/core/enums.py src/xuanshu/contracts/events.py tests/contracts/test_event_contracts.py
git commit -m "feat: add trader event contracts"
```

## Task 2: Implement OKX Websocket and REST Adapter Surface

**Files:**
- Modify: `src/xuanshu/infra/okx/public_ws.py`
- Modify: `src/xuanshu/infra/okx/private_ws.py`
- Modify: `src/xuanshu/infra/okx/rest.py`
- Test: `tests/execution/test_okx_execution_engine.py`

- [ ] **Step 1: Write failing adapter tests**

Create `tests/execution/test_okx_execution_engine.py`:

```python
from datetime import UTC, datetime

import hashlib
import hmac
import json

from xuanshu.contracts.events import OrderUpdateEvent, OrderbookTopEvent, PositionUpdateEvent
from xuanshu.core.enums import TraderEventType
from xuanshu.infra.okx.private_ws import OkxPrivateStream
from xuanshu.infra.okx.public_ws import OkxPublicStream
from xuanshu.infra.okx.rest import OkxRestClient


def test_okx_public_stream_builds_bbo_subscription_and_decodes_events() -> None:
    stream = OkxPublicStream(url="wss://ws.okx.com:8443/ws/v5/public")

    payload = stream.build_subscribe_payload(("BTC-USDT-SWAP",))
    message = {
        "arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"},
        "data": [
            {
                "ts": "1713484800000",
                "bidPx": "100.0",
                "askPx": "100.1",
                "bidSz": "5",
                "askSz": "6",
            }
        ],
    }

    event = stream.decode_message(message, sequence="pub-1")

    assert payload == {
        "op": "subscribe",
        "args": [{"channel": "tickers", "instId": "BTC-USDT-SWAP"}],
    }
    assert isinstance(event, OrderbookTopEvent)
    assert event.event_type == TraderEventType.ORDERBOOK_TOP
    assert event.bid_price == 100.0


def test_okx_private_stream_builds_login_and_decodes_order_and_position_events() -> None:
    stream = OkxPrivateStream(url="wss://ws.okx.com:8443/ws/v5/private")

    login = stream.build_login_payload(
        api_key="test-key",
        api_secret="test-secret",
        passphrase="test-passphrase",
        epoch_seconds=1713484800,
    )
    order_message = {
        "arg": {"channel": "orders", "instType": "SWAP"},
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "ordId": "1",
                "clOrdId": "btc-breakout-000001",
                "side": "buy",
                "px": "100.2",
                "sz": "1",
                "accFillSz": "0",
                "state": "live",
                "uTime": "1713484800000",
            }
        ],
    }
    position_message = {
        "arg": {"channel": "positions", "instType": "SWAP"},
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "avgPx": "100.2",
                "markPx": "100.4",
                "pos": "1",
                "upl": "0.2",
                "uTime": "1713484805000",
            }
        ],
    }

    order_event = stream.decode_message(order_message, sequence="pri-1")
    position_event = stream.decode_message(position_message, sequence="pri-2")

    expected_sign = hmac.new(
        b"test-secret",
        b"1713484800GET/users/self/verify",
        hashlib.sha256,
    ).digest()

    assert login["op"] == "login"
    assert login["args"][0]["apiKey"] == "test-key"
    assert login["args"][0]["passphrase"] == "test-passphrase"
    assert login["args"][0]["sign"] == __import__("base64").b64encode(expected_sign).decode()
    assert isinstance(order_event, OrderUpdateEvent)
    assert order_event.order_id == "1"
    assert isinstance(position_event, PositionUpdateEvent)
    assert position_event.net_quantity == 1.0


def test_okx_rest_client_builds_signed_headers_and_place_order_payload() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )

    headers = client.build_signed_headers(
        method="POST",
        path="/api/v5/trade/order",
        body='{"instId":"BTC-USDT-SWAP"}',
        timestamp="2026-04-19T00:00:00.000Z",
    )
    payload = client.build_place_order_payload(
        symbol="BTC-USDT-SWAP",
        side="buy",
        order_type="market",
        size="1",
        client_order_id="btc-breakout-000001",
    )

    assert headers["OK-ACCESS-KEY"] == "api-key"
    assert headers["OK-ACCESS-PASSPHRASE"] == "api-passphrase"
    assert "OK-ACCESS-SIGN" in headers
    assert payload == {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "cross",
        "side": "buy",
        "ordType": "market",
        "sz": "1",
        "clOrdId": "btc-breakout-000001",
    }

    __import__("asyncio").run(client.aclose())
```

- [ ] **Step 2: Run the adapter tests to verify they fail**

Run:

```bash
./.venv/bin/python -m pytest tests/execution/test_okx_execution_engine.py -v
```

Expected: FAIL because the websocket adapters and REST client do not implement these behaviors yet.

- [ ] **Step 3: Implement public stream subscription and decoding**

Update `src/xuanshu/infra/okx/public_ws.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from xuanshu.contracts.events import MarketTradeEvent, OrderbookTopEvent


@dataclass(frozen=True, slots=True)
class OkxPublicStream:
    url: str

    def build_subscribe_payload(self, symbols: tuple[str, ...]) -> dict[str, object]:
        return {
            "op": "subscribe",
            "args": [{"channel": "tickers", "instId": symbol} for symbol in symbols],
        }

    def decode_message(self, payload: dict[str, Any], sequence: str) -> OrderbookTopEvent | MarketTradeEvent | None:
        channel = payload.get("arg", {}).get("channel")
        data = payload.get("data") or []
        if not data:
            return None
        item = data[0]
        generated_at = datetime.fromtimestamp(int(item["ts"]) / 1000, tz=UTC)
        if channel == "tickers":
            return OrderbookTopEvent(
                event_type="orderbook_top",
                symbol=payload["arg"]["instId"],
                exchange="okx",
                generated_at=generated_at,
                public_sequence=sequence,
                bid_price=float(item["bidPx"]),
                ask_price=float(item["askPx"]),
                bid_size=float(item["bidSz"]),
                ask_size=float(item["askSz"]),
            )
        if channel == "trades":
            return MarketTradeEvent(
                event_type="market_trade",
                symbol=payload["arg"]["instId"],
                exchange="okx",
                generated_at=generated_at,
                public_sequence=sequence,
                price=float(item["px"]),
                size=float(item["sz"]),
                side=item["side"],
            )
        return None
```

- [ ] **Step 4: Implement private stream login and decoding**

Update `src/xuanshu/infra/okx/private_ws.py`:

```python
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from xuanshu.contracts.events import AccountSnapshotEvent, OrderUpdateEvent, PositionUpdateEvent


@dataclass(frozen=True, slots=True)
class OkxPrivateStream:
    url: str

    def build_login_payload(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        epoch_seconds: int,
    ) -> dict[str, object]:
        prehash = f"{epoch_seconds}GET/users/self/verify".encode()
        signature = base64.b64encode(hmac.new(api_secret.encode(), prehash, hashlib.sha256).digest()).decode()
        return {
            "op": "login",
            "args": [
                {
                    "apiKey": api_key,
                    "passphrase": passphrase,
                    "timestamp": str(epoch_seconds),
                    "sign": signature,
                }
            ],
        }

    def decode_message(
        self,
        payload: dict[str, Any],
        sequence: str,
    ) -> OrderUpdateEvent | PositionUpdateEvent | AccountSnapshotEvent | None:
        channel = payload.get("arg", {}).get("channel")
        data = payload.get("data") or []
        if not data:
            return None
        item = data[0]
        generated_at = datetime.fromtimestamp(int(item["uTime"]) / 1000, tz=UTC)
        if channel == "orders":
            return OrderUpdateEvent(
                event_type="order_update",
                symbol=item["instId"],
                exchange="okx",
                generated_at=generated_at,
                private_sequence=sequence,
                order_id=item["ordId"],
                client_order_id=item["clOrdId"],
                side=item["side"],
                price=float(item["px"]),
                size=float(item["sz"]),
                filled_size=float(item["accFillSz"]),
                status=item["state"],
            )
        if channel == "positions":
            return PositionUpdateEvent(
                event_type="position_update",
                symbol=item["instId"],
                exchange="okx",
                generated_at=generated_at,
                private_sequence=sequence,
                net_quantity=float(item["pos"]),
                average_price=float(item["avgPx"]),
                mark_price=float(item["markPx"]),
                unrealized_pnl=float(item["upl"]),
            )
        if channel == "account":
            return AccountSnapshotEvent(
                event_type="account_snapshot",
                exchange="okx",
                generated_at=generated_at,
                private_sequence=sequence,
                equity=float(item["totalEq"]),
                available_balance=float(item["availEq"]),
                margin_ratio=float(item.get("mgnRatio", 0.0)),
            )
        return None
```

- [ ] **Step 5: Implement signed REST helpers and place-order payload construction**

Update `src/xuanshu/infra/okx/rest.py`:

```python
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx


class OkxRestClient:
    ...

    def build_signed_headers(
        self,
        method: str,
        path: str,
        body: str,
        timestamp: str,
    ) -> dict[str, str]:
        if not self.api_secret or not self.passphrase:
            raise ValueError("api_secret and passphrase are required for signed requests")
        message = f"{timestamp}{method.upper()}{path}{body}".encode()
        signature = base64.b64encode(hmac.new(self.api_secret.encode(), message, hashlib.sha256).digest()).decode()
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-SIGN": signature,
            "Content-Type": "application/json",
        }

    def build_place_order_payload(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: str,
        client_order_id: str,
        price: str | None = None,
    ) -> dict[str, str]:
        payload = {
            "instId": symbol,
            "tdMode": "cross",
            "side": side,
            "ordType": order_type,
            "sz": size,
            "clOrdId": client_order_id,
        }
        if price is not None:
            payload["px"] = price
        return payload

    async def place_order(self, payload: dict[str, str], timestamp: str) -> dict[str, object]:
        body = json.dumps(payload, separators=(",", ":"))
        headers = self.build_signed_headers("POST", "/api/v5/trade/order", body, timestamp)
        response = await self.client.post("/api/v5/trade/order", content=body, headers=headers)
        response.raise_for_status()
        return response.json()
```

- [ ] **Step 6: Run the adapter tests to verify they pass**

Run:

```bash
./.venv/bin/python -m pytest tests/execution/test_okx_execution_engine.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit the adapter surface**

Run:

```bash
git add src/xuanshu/infra/okx/public_ws.py src/xuanshu/infra/okx/private_ws.py src/xuanshu/infra/okx/rest.py tests/execution/test_okx_execution_engine.py
git commit -m "feat: add okx live adapter surface"
```

## Task 3: Expand State Engine and Storage Boundaries

**Files:**
- Modify: `src/xuanshu/state/engine.py`
- Modify: `src/xuanshu/infra/storage/redis_store.py`
- Modify: `src/xuanshu/infra/storage/postgres_store.py`
- Test: `tests/storage/test_storage_boundaries.py`
- Test: `tests/trader/test_dispatcher.py`

- [ ] **Step 1: Add failing state/storage tests**

Create `tests/trader/test_dispatcher.py`:

```python
from datetime import UTC, datetime

from xuanshu.contracts.events import FaultEvent, OrderUpdateEvent, OrderbookTopEvent, PositionUpdateEvent
from xuanshu.core.enums import RunMode, TraderEventType
from xuanshu.state.engine import StateEngine


def test_state_engine_tracks_market_orders_positions_mode_and_faults() -> None:
    engine = StateEngine()

    engine.on_orderbook_top(
        OrderbookTopEvent(
            event_type=TraderEventType.ORDERBOOK_TOP,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            public_sequence="pub-1",
            bid_price=100.0,
            ask_price=100.1,
            bid_size=5.0,
            ask_size=6.0,
        )
    )
    engine.on_order_update(
        OrderUpdateEvent(
            event_type=TraderEventType.ORDER_UPDATE,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            private_sequence="pri-1",
            order_id="ord-1",
            client_order_id="btc-breakout-000001",
            side="buy",
            price=100.1,
            size=1.0,
            filled_size=0.0,
            status="live",
        )
    )
    engine.on_position_update(
        PositionUpdateEvent(
            event_type=TraderEventType.POSITION_UPDATE,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            private_sequence="pri-2",
            net_quantity=1.0,
            average_price=100.1,
            mark_price=100.2,
            unrealized_pnl=0.1,
        )
    )
    engine.on_fault(
        FaultEvent(
            event_type=TraderEventType.RUNTIME_FAULT,
            exchange="okx",
            generated_at=datetime.now(UTC),
            severity="warn",
            code="public_ws_disconnected",
            detail="public stream dropped",
        )
    )
    engine.set_run_mode(RunMode.REDUCE_ONLY)

    summary = engine.build_symbol_runtime_summary("BTC-USDT-SWAP")

    assert summary["symbol"] == "BTC-USDT-SWAP"
    assert summary["open_order_count"] == 1
    assert summary["net_quantity"] == 1.0
    assert summary["run_mode"] == "reduce_only"
    assert "public_ws_disconnected" in engine.fault_flags
```

Append to `tests/storage/test_storage_boundaries.py`:

```python
def test_redis_runtime_summary_and_fault_store_round_trips_json() -> None:
    store = RedisRuntimeStateStore(redis_client=_FakeRedis())

    store.set_symbol_runtime_summary(
        "BTC-USDT-SWAP",
        {"symbol": "BTC-USDT-SWAP", "run_mode": "normal", "net_quantity": 1.0},
    )
    store.set_fault_flags({"public_ws_disconnected": {"severity": "warn"}})

    assert store.get_symbol_runtime_summary("BTC-USDT-SWAP") == {
        "symbol": "BTC-USDT-SWAP",
        "run_mode": "normal",
        "net_quantity": 1.0,
    }
    assert store.get_fault_flags() == {"public_ws_disconnected": {"severity": "warn"}}


def test_postgres_store_exposes_append_fact_methods() -> None:
    store = __import__("xuanshu.infra.storage.postgres_store", fromlist=["PostgresRuntimeStore"]).PostgresRuntimeStore(
        dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu"
    )

    assert hasattr(store, "append_order_fact")
    assert hasattr(store, "append_fill_fact")
    assert hasattr(store, "append_position_fact")
    assert hasattr(store, "append_risk_event")
    assert hasattr(store, "save_checkpoint")
```

- [ ] **Step 2: Run the new state/storage tests to verify they fail**

Run:

```bash
./.venv/bin/python -m pytest tests/trader/test_dispatcher.py tests/storage/test_storage_boundaries.py -v
```

Expected: FAIL due to missing state-engine methods and missing storage write paths.

- [ ] **Step 3: Expand state engine**

Update `src/xuanshu/state/engine.py` to add tracked order/position state, run mode, markers, and fault flags:

```python
@dataclass
class OrderState:
    order_id: str
    client_order_id: str
    side: str
    price: float
    size: float
    filled_size: float
    status: str


@dataclass
class PositionState:
    net_quantity: float = 0.0
    average_price: float = 0.0
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class StateEngine:
    symbols: dict[str, SymbolState] = field(default_factory=dict)
    open_orders_by_symbol: dict[str, dict[str, OrderState]] = field(default_factory=dict)
    positions_by_symbol: dict[str, PositionState] = field(default_factory=dict)
    fault_flags: dict[str, dict[str, str]] = field(default_factory=dict)
    current_run_mode: RunMode = RunMode.NORMAL
    last_public_stream_marker: str | None = None
    last_private_stream_marker: str | None = None
    ...

    def set_run_mode(self, mode: RunMode) -> None:
        self.current_run_mode = mode

    def on_orderbook_top(self, event: OrderbookTopEvent) -> None:
        self.last_public_stream_marker = event.public_sequence
        self.on_bbo(event.symbol, bid=event.bid_price, ask=event.ask_price)

    def on_order_update(self, event: OrderUpdateEvent) -> None:
        self.last_private_stream_marker = event.private_sequence
        symbol_orders = self.open_orders_by_symbol.setdefault(event.symbol, {})
        symbol_orders[event.order_id] = OrderState(
            order_id=event.order_id,
            client_order_id=event.client_order_id,
            side=event.side,
            price=event.price,
            size=event.size,
            filled_size=event.filled_size,
            status=event.status,
        )
        if event.status in {"filled", "canceled"}:
            symbol_orders.pop(event.order_id, None)

    def on_position_update(self, event: PositionUpdateEvent) -> None:
        self.last_private_stream_marker = event.private_sequence
        self.positions_by_symbol[event.symbol] = PositionState(
            net_quantity=event.net_quantity,
            average_price=event.average_price,
            mark_price=event.mark_price,
            unrealized_pnl=event.unrealized_pnl,
        )

    def on_fault(self, event: FaultEvent) -> None:
        self.fault_flags[event.code] = {"severity": event.severity, "detail": event.detail}

    def build_symbol_runtime_summary(self, symbol: str) -> dict[str, object]:
        position = self.positions_by_symbol.get(symbol, PositionState())
        open_orders = self.open_orders_by_symbol.get(symbol, {})
        snapshot = self.snapshot(symbol)
        return {
            "symbol": symbol,
            "run_mode": self.current_run_mode.value,
            "mid_price": snapshot.mid_price,
            "spread": snapshot.spread,
            "regime": snapshot.regime.value,
            "net_quantity": position.net_quantity,
            "open_order_count": len(open_orders),
            "fault_count": len(self.fault_flags),
        }
```

- [ ] **Step 4: Extend Redis and PostgreSQL storage boundaries**

Update `src/xuanshu/infra/storage/redis_store.py`:

```python
import json
...

    @staticmethod
    def fault_flags() -> str:
        return "xuanshu:runtime:fault_flags"


class RedisRuntimeStateStore:
    ...
    def set_symbol_runtime_summary(self, symbol: str, summary: dict[str, object]) -> None:
        try:
            self._redis.set(RedisKeys.symbol_runtime(symbol), json.dumps(summary, separators=(",", ":")))
        except RedisError:
            return

    def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
        payload = self._redis.get(RedisKeys.symbol_runtime(symbol))
        if payload is None:
            return None
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return json.loads(payload)

    def set_fault_flags(self, flags: dict[str, object]) -> None:
        try:
            self._redis.set(RedisKeys.fault_flags(), json.dumps(flags, separators=(",", ":")))
        except RedisError:
            return

    def get_fault_flags(self) -> dict[str, object] | None:
        payload = self._redis.get(RedisKeys.fault_flags())
        if payload is None:
            return None
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return json.loads(payload)
```

Update `src/xuanshu/infra/storage/postgres_store.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

POSTGRES_TABLES = (...)


@dataclass
class PostgresRuntimeStore:
    dsn: str
    written_rows: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {table: [] for table in POSTGRES_TABLES}
    )

    def append_order_fact(self, payload: dict[str, Any]) -> None:
        self.written_rows["orders"].append(payload)

    def append_fill_fact(self, payload: dict[str, Any]) -> None:
        self.written_rows["fills"].append(payload)

    def append_position_fact(self, payload: dict[str, Any]) -> None:
        self.written_rows["positions"].append(payload)

    def append_risk_event(self, payload: dict[str, Any]) -> None:
        self.written_rows["risk_events"].append(payload)

    def save_checkpoint(self, payload: dict[str, Any]) -> None:
        self.written_rows["execution_checkpoints"].append(payload)
```

- [ ] **Step 5: Run the state/storage tests to verify they pass**

Run:

```bash
./.venv/bin/python -m pytest tests/trader/test_dispatcher.py tests/storage/test_storage_boundaries.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit the state/storage foundation**

Run:

```bash
git add src/xuanshu/state/engine.py src/xuanshu/infra/storage/redis_store.py src/xuanshu/infra/storage/postgres_store.py tests/trader/test_dispatcher.py tests/storage/test_storage_boundaries.py
git commit -m "feat: add trader runtime state and storage summaries"
```

## Task 4: Add Execution Coordinator and Recovery Supervisor

**Files:**
- Modify: `src/xuanshu/execution/engine.py`
- Create: `src/xuanshu/execution/coordinator.py`
- Create: `src/xuanshu/trader/recovery.py`
- Test: `tests/execution/test_execution_coordinator.py`
- Test: `tests/trader/test_recovery.py`

- [ ] **Step 1: Write failing coordinator and recovery tests**

Create `tests/execution/test_execution_coordinator.py`:

```python
from datetime import UTC, datetime

import pytest

from xuanshu.contracts.risk import RiskDecision
from xuanshu.core.enums import RunMode
from xuanshu.execution.coordinator import ExecutionCoordinator


class _FakeRestClient:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, str], str]] = []

    async def place_order(self, payload: dict[str, str], timestamp: str) -> dict[str, object]:
        self.calls.append((payload, timestamp))
        return {"data": [{"ordId": "1", "clOrdId": payload["clOrdId"], "sCode": "0"}]}


@pytest.mark.asyncio
async def test_execution_coordinator_places_idempotent_order_once() -> None:
    rest = _FakeRestClient()
    coordinator = ExecutionCoordinator(rest_client=rest)
    decision = RiskDecision(
        decision_id="dec-1",
        generated_at=datetime.now(UTC),
        symbol="BTC-USDT-SWAP",
        allow_open=True,
        allow_close=True,
        max_position=100.0,
        max_order_size=1.0,
        risk_mode=RunMode.NORMAL,
        reason_codes=[],
    )

    await coordinator.submit_market_open(
        symbol="BTC-USDT-SWAP",
        side="buy",
        size=1.0,
        client_order_id="btc-breakout-000001",
        decision=decision,
        timestamp="2026-04-19T00:00:00.000Z",
    )
    await coordinator.submit_market_open(
        symbol="BTC-USDT-SWAP",
        side="buy",
        size=1.0,
        client_order_id="btc-breakout-000001",
        decision=decision,
        timestamp="2026-04-19T00:00:00.000Z",
    )

    assert len(rest.calls) == 1
    assert coordinator.inflight_by_client_order_id["btc-breakout-000001"]["symbol"] == "BTC-USDT-SWAP"
```

Create `tests/trader/test_recovery.py`:

```python
from datetime import UTC, datetime

from xuanshu.contracts.checkpoint import CheckpointBudgetState, ExecutionCheckpoint
from xuanshu.core.enums import RunMode
from xuanshu.trader.recovery import RecoverySupervisor


class _FakeRestClient:
    async def fetch_open_orders(self, symbol: str) -> list[dict[str, object]]:
        return [{"order_id": "ord-1", "symbol": symbol}]

    async def fetch_positions(self, symbol: str) -> list[dict[str, object]]:
        return [{"symbol": symbol, "net_quantity": 1.0}]

    async def fetch_account_summary(self) -> dict[str, object]:
        return {"equity": 1000.0, "available_balance": 800.0}


async def test_recovery_supervisor_blocks_when_checkpoint_and_exchange_diverge() -> None:
    checkpoint = ExecutionCheckpoint(
        checkpoint_id="cp-1",
        created_at=datetime.now(UTC),
        active_snapshot_version="snap-1",
        current_mode=RunMode.NORMAL,
        positions_snapshot=[],
        open_orders_snapshot=[],
        budget_state=CheckpointBudgetState(
            max_daily_loss=100.0,
            remaining_daily_loss=80.0,
            remaining_notional=60.0,
            remaining_order_count=10,
        ),
        last_public_stream_marker="pub-1",
        last_private_stream_marker="pri-1",
        needs_reconcile=False,
    )

    supervisor = RecoverySupervisor(rest_client=_FakeRestClient())
    result = await supervisor.run_startup_recovery("BTC-USDT-SWAP", checkpoint)

    assert result["run_mode"] == "halted"
    assert result["needs_reconcile"] is True
    assert result["reason"] == "exchange_state_mismatch"
```

- [ ] **Step 2: Run the coordinator and recovery tests to verify they fail**

Run:

```bash
./.venv/bin/python -m pytest tests/execution/test_execution_coordinator.py tests/trader/test_recovery.py -v
```

Expected: FAIL because coordinator and recovery modules do not exist yet.

- [ ] **Step 3: Extend execution engine with pure payload builders**

Update `src/xuanshu/execution/engine.py`:

```python
def build_market_order_payload(symbol: str, side: str, size: float, client_order_id: str) -> dict[str, str]:
    _validate_component("symbol", symbol, _SYMBOL_PATTERN)
    if side not in {"buy", "sell"}:
        raise ValueError(f"invalid side: {side!r}")
    if size <= 0:
        raise ValueError(f"invalid size: {size!r}")
    return {
        "instId": symbol,
        "tdMode": "cross",
        "side": side,
        "ordType": "market",
        "sz": f"{size:g}",
        "clOrdId": client_order_id,
    }
```

- [ ] **Step 4: Implement execution coordinator**

Create `src/xuanshu/execution/coordinator.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from xuanshu.contracts.risk import RiskDecision
from xuanshu.execution.engine import build_market_order_payload


class ExecutionRestClient(Protocol):
    async def place_order(self, payload: dict[str, str], timestamp: str) -> dict[str, object]:
        ...


@dataclass
class ExecutionCoordinator:
    rest_client: ExecutionRestClient
    inflight_by_client_order_id: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def submit_market_open(
        self,
        symbol: str,
        side: str,
        size: float,
        client_order_id: str,
        decision: RiskDecision,
        timestamp: str,
    ) -> dict[str, object] | None:
        if not decision.allow_open:
            return None
        if client_order_id in self.inflight_by_client_order_id:
            return self.inflight_by_client_order_id[client_order_id]["response"]
        payload = build_market_order_payload(symbol, side, size, client_order_id)
        response = await self.rest_client.place_order(payload, timestamp)
        self.inflight_by_client_order_id[client_order_id] = {
            "symbol": symbol,
            "payload": payload,
            "response": response,
        }
        return response
```

- [ ] **Step 5: Implement recovery supervisor**

Create `src/xuanshu/trader/recovery.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from xuanshu.contracts.checkpoint import ExecutionCheckpoint


class RecoveryRestClient(Protocol):
    async def fetch_open_orders(self, symbol: str) -> list[dict[str, object]]:
        ...

    async def fetch_positions(self, symbol: str) -> list[dict[str, object]]:
        ...

    async def fetch_account_summary(self) -> dict[str, object]:
        ...


@dataclass
class RecoverySupervisor:
    rest_client: RecoveryRestClient

    async def run_startup_recovery(self, symbol: str, checkpoint: ExecutionCheckpoint) -> dict[str, object]:
        open_orders = await self.rest_client.fetch_open_orders(symbol)
        positions = await self.rest_client.fetch_positions(symbol)
        await self.rest_client.fetch_account_summary()
        checkpoint_orders = len(checkpoint.open_orders_snapshot)
        checkpoint_positions = len(checkpoint.positions_snapshot)
        if len(open_orders) != checkpoint_orders or len(positions) != checkpoint_positions:
            return {
                "run_mode": "halted",
                "needs_reconcile": True,
                "reason": "exchange_state_mismatch",
            }
        return {
            "run_mode": checkpoint.current_mode.value,
            "needs_reconcile": False,
            "reason": "checkpoint_matches_exchange",
        }
```

- [ ] **Step 6: Run the coordinator and recovery tests to verify they pass**

Run:

```bash
./.venv/bin/python -m pytest tests/execution/test_execution_coordinator.py tests/trader/test_recovery.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit coordinator and recovery**

Run:

```bash
git add src/xuanshu/execution/engine.py src/xuanshu/execution/coordinator.py src/xuanshu/trader/recovery.py tests/execution/test_execution_coordinator.py tests/trader/test_recovery.py
git commit -m "feat: add execution coordinator and trader recovery"
```

## Task 5: Wire the Trader Runtime Into a Real Event Loop

**Files:**
- Create: `src/xuanshu/trader/dispatcher.py`
- Modify: `src/xuanshu/apps/trader.py`
- Modify: `tests/apps/test_trader_app_wiring.py`

- [ ] **Step 1: Write failing trader runtime wiring tests**

Append to `tests/apps/test_trader_app_wiring.py`:

```python
import asyncio
from datetime import UTC, datetime

from xuanshu.contracts.events import OrderbookTopEvent
from xuanshu.core.enums import TraderEventType


def test_trader_runtime_dispatches_market_event_updates_summary_and_mode(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    monkeypatch.setattr(
        trader_app,
        "build_snapshot_store",
        lambda settings: RedisSnapshotStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(
        trader_app,
        "build_runtime_state_store",
        lambda settings: RedisRuntimeStateStore(redis_client=fake_redis),
    )

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)
    runtime = trader_app.build_trader_runtime()

    async def _exercise_runtime() -> None:
        try:
            await trader_app._dispatch_runtime_event(
                runtime,
                OrderbookTopEvent(
                    event_type=TraderEventType.ORDERBOOK_TOP,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-1",
                    bid_price=100.0,
                    ask_price=100.1,
                    bid_size=5.0,
                    ask_size=6.0,
                ),
            )
        finally:
            await runtime.components.aclose()

    asyncio.run(_exercise_runtime())

    assert runtime.runtime_store.get_symbol_runtime_summary("BTC-USDT-SWAP")["symbol"] == "BTC-USDT-SWAP"
    assert runtime.runtime_store.get_run_mode() in {RunMode.NORMAL, RunMode.DEGRADED, RunMode.REDUCE_ONLY, RunMode.HALTED}
```

- [ ] **Step 2: Run the trader runtime wiring test to verify it fails**

Run:

```bash
./.venv/bin/python -m pytest tests/apps/test_trader_app_wiring.py::test_trader_runtime_dispatches_market_event_updates_summary_and_mode -v
```

Expected: FAIL because dispatcher wiring and runtime publishing do not exist.

- [ ] **Step 3: Implement dispatcher**

Create `src/xuanshu/trader/dispatcher.py`:

```python
from __future__ import annotations

from xuanshu.contracts.events import FaultEvent, OrderUpdateEvent, OrderbookTopEvent, PositionUpdateEvent
from xuanshu.state.engine import StateEngine


def dispatch_event(engine: StateEngine, event: object) -> None:
    if isinstance(event, OrderbookTopEvent):
        engine.on_orderbook_top(event)
        return
    if isinstance(event, OrderUpdateEvent):
        engine.on_order_update(event)
        return
    if isinstance(event, PositionUpdateEvent):
        engine.on_position_update(event)
        return
    if isinstance(event, FaultEvent):
        engine.on_fault(event)
        return
```

- [ ] **Step 4: Wire trader app runtime dispatch and summary publication**

Update `src/xuanshu/apps/trader.py`:

```python
from xuanshu.execution.coordinator import ExecutionCoordinator
from xuanshu.trader.dispatcher import dispatch_event
from xuanshu.trader.recovery import RecoverySupervisor
...

@dataclass(slots=True)
class TraderRuntime:
    ...
    execution_coordinator: ExecutionCoordinator
    recovery_supervisor: RecoverySupervisor


async def _dispatch_runtime_event(runtime: TraderRuntime, event: object) -> None:
    dispatch_event(runtime.components.state_engine, event)
    symbol = getattr(event, "symbol", None)
    if symbol:
        runtime.runtime_store.set_symbol_runtime_summary(
            symbol,
            runtime.components.state_engine.build_symbol_runtime_summary(symbol),
        )
    runtime.runtime_store.set_run_mode(runtime.components.state_engine.current_run_mode)
    runtime.runtime_store.set_fault_flags(runtime.components.state_engine.fault_flags)


def build_trader_runtime() -> TraderRuntime:
    ...
    components = build_trader_components(settings)
    return TraderRuntime(
        ...
        execution_coordinator=ExecutionCoordinator(rest_client=components.okx_rest_client),
        recovery_supervisor=RecoverySupervisor(rest_client=components.okx_rest_client),
    )
```

- [ ] **Step 5: Run the trader runtime wiring test to verify it passes**

Run:

```bash
./.venv/bin/python -m pytest tests/apps/test_trader_app_wiring.py::test_trader_runtime_dispatches_market_event_updates_summary_and_mode -v
```

Expected: PASS.

- [ ] **Step 6: Run the broader trader test slice**

Run:

```bash
./.venv/bin/python -m pytest tests/apps/test_trader_app_wiring.py tests/trader/test_trader_decision_flow.py tests/trader/test_dispatcher.py tests/trader/test_recovery.py tests/execution/test_execution_coordinator.py tests/execution/test_okx_execution_engine.py tests/storage/test_storage_boundaries.py tests/contracts/test_event_contracts.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit the trader runtime loop wiring**

Run:

```bash
git add src/xuanshu/trader/dispatcher.py src/xuanshu/apps/trader.py tests/apps/test_trader_app_wiring.py
git commit -m "feat: wire trader live runtime loop"
```

## Self-Review

Spec coverage check:

- normalized event model: covered by Task 1
- OKX live adapter surface: covered by Task 2
- state engine and Redis/PostgreSQL runtime summaries: covered by Task 3
- execution coordinator and startup recovery: covered by Task 4
- trader app runtime wiring: covered by Task 5

Known intentional limits in this plan:

- PostgreSQL persistence is append-oriented and minimal rather than full SQLAlchemy schema work
- websocket connection management is planned through adapter helpers and runtime wiring, not a full production reconnect supervisor in one task
- symbol support is generic to `USDT-SWAP`, but validation scenarios remain focused on `BTC/ETH`

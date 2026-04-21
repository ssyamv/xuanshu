import pytest
from sqlalchemy.exc import SQLAlchemyError

from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.infra.storage.postgres_store import POSTGRES_TABLES, PostgresRuntimeStore
from xuanshu.infra.storage.redis_store import RedisKeys, RedisRuntimeStateStore, RedisSnapshotStore


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def set(self, key: str, value: str) -> bool:
        self.values[key] = value.encode("utf-8")
        return True

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    def delete(self, key: str) -> int:
        self.values.pop(key, None)
        return 1


def _build_snapshot(version_id: str = "snap-001") -> StrategyConfigSnapshot:
    return StrategyConfigSnapshot.model_validate(
        {
            "version_id": version_id,
            "generated_at": "2026-04-18T00:00:00Z",
            "effective_from": "2026-04-18T00:00:00Z",
            "expires_at": "2026-04-18T00:05:00Z",
            "symbol_whitelist": ["ETH-USDT-SWAP"],
            "strategy_enable_flags": {"vol_breakout": True},
            "risk_multiplier": 0.5,
            "per_symbol_max_position": 0.12,
            "max_leverage": 3,
            "market_mode": RunMode.NORMAL,
            "approval_state": ApprovalState.APPROVED,
            "source_reason": "fixed strategy",
            "ttl_sec": 300,
        }
    )


def test_redis_key_naming_matches_runtime_contract() -> None:
    assert RedisKeys.latest_snapshot() == "xuanshu:strategy:latest"
    assert RedisKeys.run_mode() == "xuanshu:runtime:mode"
    assert RedisKeys.symbol_runtime("ETH-USDT-SWAP") == "xuanshu:runtime:symbol:ETH-USDT-SWAP"
    assert (
        RedisKeys.active_symbol_strategy("ETH-USDT-SWAP")
        == "xuanshu:runtime:active_strategy:ETH-USDT-SWAP"
    )
    assert RedisKeys.budget_pool_summary() == "xuanshu:runtime:budget_pool"
    assert RedisKeys.fault_flags() == "xuanshu:runtime:fault_flags"
    assert RedisKeys.manual_release_target() == "xuanshu:runtime:manual_release_target"


def test_redis_symbol_runtime_rejects_unsafe_input() -> None:
    with pytest.raises(ValueError):
        RedisKeys.symbol_runtime("eth/usdt swap")


def test_redis_active_symbol_strategy_rejects_unsafe_input() -> None:
    with pytest.raises(ValueError):
        RedisKeys.active_symbol_strategy("eth/usdt swap")


def test_redis_snapshot_store_keeps_only_the_latest_snapshot() -> None:
    store = RedisSnapshotStore(redis_client=_FakeRedis())
    first_snapshot = _build_snapshot("v1")
    second_snapshot = _build_snapshot("v2")

    store.set_latest_snapshot("v1", first_snapshot)
    store.set_latest_snapshot("v2", second_snapshot)

    assert store.latest_version_id == "v2"
    assert store.get_latest_snapshot() == second_snapshot


def test_redis_snapshot_store_returns_none_for_invalid_payload() -> None:
    client = _FakeRedis()
    client.set(RedisKeys.latest_snapshot(), "{not-json")
    store = RedisSnapshotStore(redis_client=client)

    assert store.get_latest_snapshot() is None


def test_redis_snapshot_store_ignores_malformed_non_utf8_bytes() -> None:
    client = _FakeRedis()
    client.values[RedisKeys.latest_snapshot()] = b"\xff\xfe\xfd"
    store = RedisSnapshotStore(redis_client=client)

    assert store.get_latest_snapshot() is None


def test_redis_runtime_state_store_round_trips_runtime_json() -> None:
    store = RedisRuntimeStateStore(redis_client=_FakeRedis())

    store.set_run_mode(RunMode.REDUCE_ONLY)
    store.set_symbol_runtime_summary(
        "ETH-USDT-SWAP",
        {"symbol": "ETH-USDT-SWAP", "run_mode": "normal", "net_quantity": 1.0},
    )
    store.set_fault_flags({"public_ws_disconnected": {"severity": "warn"}})
    store.set_budget_pool_summary(
        {"remaining_notional": 100.0, "remaining_order_count": 10, "current_mode": "normal"}
    )
    store.set_manual_release_target("degraded")

    assert store.get_run_mode() == RunMode.REDUCE_ONLY
    assert store.get_symbol_runtime_summary("ETH-USDT-SWAP") == {
        "symbol": "ETH-USDT-SWAP",
        "run_mode": "normal",
        "net_quantity": 1.0,
    }
    assert store.get_fault_flags() == {"public_ws_disconnected": {"severity": "warn"}}
    assert store.get_budget_pool_summary() == {
        "remaining_notional": 100.0,
        "remaining_order_count": 10,
        "current_mode": "normal",
    }
    assert store.get_manual_release_target() == "degraded"

    store.clear_manual_release_target()

    assert store.get_manual_release_target() is None


def test_redis_runtime_state_store_ignores_malformed_json() -> None:
    client = _FakeRedis()
    client.set(RedisKeys.symbol_runtime("ETH-USDT-SWAP"), "{not-json")
    client.set(RedisKeys.fault_flags(), "{not-json")
    store = RedisRuntimeStateStore(redis_client=client)

    assert store.get_symbol_runtime_summary("ETH-USDT-SWAP") is None
    assert store.get_fault_flags() is None


def test_redis_runtime_state_store_ignores_malformed_non_utf8_run_mode_bytes() -> None:
    client = _FakeRedis()
    client.values[RedisKeys.run_mode()] = b"\xff\xfe\xfd"
    store = RedisRuntimeStateStore(redis_client=client)

    assert store.get_run_mode() is None


def test_postgres_store_exposes_runtime_fact_methods() -> None:
    store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")

    assert hasattr(store, "append_order_fact")
    assert hasattr(store, "append_fill_fact")
    assert hasattr(store, "append_position_fact")
    assert hasattr(store, "append_risk_event")
    assert hasattr(store, "append_strategy_snapshot")
    assert hasattr(store, "save_checkpoint")
    assert hasattr(store, "append_notification_event")
    assert hasattr(store, "append_strategy_replacement")
    assert hasattr(store, "list_recent_rows")


@pytest.mark.parametrize(
    ("method_name", "table_name"),
    [
        ("append_order_fact", "orders"),
        ("append_fill_fact", "fills"),
        ("append_position_fact", "positions"),
        ("append_risk_event", "risk_events"),
        ("append_strategy_snapshot", "strategy_snapshots"),
        ("save_checkpoint", "execution_checkpoints"),
        ("append_notification_event", "notification_events"),
        ("append_strategy_replacement", "strategy_replacements"),
    ],
)
def test_postgres_store_copies_payloads_before_append(method_name: str, table_name: str) -> None:
    store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    payload = {"event_id": "evt-1", "detail": {"status": "live"}, "values": [1, 2]}

    getattr(store, method_name)(payload)
    payload["event_id"] = "evt-2"
    payload["detail"]["status"] = "mutated"
    payload["values"].append(3)

    assert store.written_rows[table_name] == [
        {"event_id": "evt-1", "detail": {"status": "live"}, "values": [1, 2]}
    ]


def test_postgres_tables_are_deterministic_and_immutable() -> None:
    assert POSTGRES_TABLES == (
        "orders",
        "fills",
        "positions",
        "risk_events",
        "strategy_snapshots",
        "execution_checkpoints",
        "notification_events",
        "strategy_replacements",
    )


def test_postgres_runtime_store_appends_strategy_replacement_rows() -> None:
    store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")

    store.append_strategy_replacement(
        {
            "symbol": "ETH-USDT-SWAP",
            "current_strategy_def_id": "strat-old",
            "next_strategy_def_id": "strat-new",
            "score_delta_percent": 12.5,
        }
    )

    assert store.list_recent_rows("strategy_replacements", limit=1)[0] == {
        "symbol": "ETH-USDT-SWAP",
        "current_strategy_def_id": "strat-old",
        "next_strategy_def_id": "strat-new",
        "score_delta_percent": 12.5,
    }


def test_postgres_store_can_append_and_list_notification_events() -> None:
    store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    store.append_notification_event(
        {
            "category": "mode_change",
            "severity": "CRITICAL",
            "status": "failed",
            "attempt_count": 3,
            "needs_retry": True,
        }
    )

    assert store.list_recent_rows("notification_events", limit=1) == [
        {
            "category": "mode_change",
            "severity": "CRITICAL",
            "status": "failed",
            "attempt_count": 3,
            "needs_retry": True,
        }
    ]


def test_postgres_store_persists_rows_across_instances_with_real_sqlite_backend(tmp_path) -> None:
    dsn = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    first = PostgresRuntimeStore(dsn=dsn)
    first.append_order_fact(
        {
            "symbol": "ETH-USDT-SWAP",
            "side": "buy",
            "status": "filled",
            "client_order_id": "eth-volbreakout-000001",
        }
    )
    first.append_notification_event(
        {
            "category": "mode_change",
            "severity": "CRITICAL",
            "status": "sent",
            "attempt_count": 1,
            "needs_retry": False,
            "text": "进入 reduce_only 模式",
        }
    )

    second = PostgresRuntimeStore(dsn=dsn)

    order_rows = second.list_recent_rows("orders", limit=1)
    notification_rows = second.list_recent_rows("notification_events", limit=1)

    assert len(order_rows) == 1
    assert order_rows[0] == {
        "symbol": "ETH-USDT-SWAP",
        "side": "buy",
        "status": "filled",
        "client_order_id": "eth-volbreakout-000001",
        "created_at": order_rows[0]["created_at"],
    }
    assert len(notification_rows) == 1
    assert notification_rows[0] == {
        "category": "mode_change",
        "severity": "CRITICAL",
        "status": "sent",
        "attempt_count": 1,
        "needs_retry": False,
        "text": "进入 reduce_only 模式",
        "created_at": notification_rows[0]["created_at"],
    }


def test_postgres_store_backfills_created_at_when_loading_persisted_rows(tmp_path) -> None:
    dsn = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    first = PostgresRuntimeStore(dsn=dsn)
    first.append_order_fact(
        {
            "symbol": "ETH-USDT-SWAP",
            "side": "buy",
            "status": "filled",
            "client_order_id": "eth-volbreakout-000001",
        }
    )

    second = PostgresRuntimeStore(dsn=dsn)

    rows = second.list_recent_rows("orders", limit=1)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "ETH-USDT-SWAP"
    assert rows[0]["created_at"].endswith("+00:00") or rows[0]["created_at"].endswith("Z")


def test_postgres_store_falls_back_to_memory_when_runtime_write_fails(tmp_path) -> None:
    dsn = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    store = PostgresRuntimeStore(dsn=dsn)
    store._ensure_database()

    class _BrokenConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *args, **kwargs):
            raise SQLAlchemyError("write failed")

    class _BrokenEngine:
        def begin(self):
            return _BrokenConnection()

    store._engine = _BrokenEngine()

    store.append_risk_event({"event_type": "runtime_mode_changed", "detail": "degraded"})

    assert store.written_rows["risk_events"] == [
        {"event_type": "runtime_mode_changed", "detail": "degraded"}
    ]


def test_postgres_store_falls_back_to_memory_when_runtime_read_fails(tmp_path) -> None:
    dsn = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    store = PostgresRuntimeStore(dsn=dsn)
    store.append_risk_event({"event_type": "runtime_mode_changed", "detail": "degraded"})
    store._ensure_database()

    class _BrokenConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *args, **kwargs):
            raise SQLAlchemyError("read failed")

    class _BrokenEngine:
        def begin(self):
            return _BrokenConnection()

    store._engine = _BrokenEngine()

    assert store.list_recent_rows("risk_events", limit=1) == [
        {"event_type": "runtime_mode_changed", "detail": "degraded"}
    ]

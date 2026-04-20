import pytest
from sqlalchemy.exc import SQLAlchemyError

from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.infra.storage.postgres_store import POSTGRES_TABLES, PostgresRuntimeStore
from xuanshu.infra.storage.qdrant_store import QDRANT_COLLECTIONS, QdrantCaseStore
from xuanshu.infra.storage.redis_store import RedisKeys, RedisRuntimeStateStore, RedisSnapshotStore


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def set(self, key: str, value: str) -> bool:
        self.values[key] = value.encode("utf-8")
        return True

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)


def _build_snapshot(version_id: str = "snap-001") -> StrategyConfigSnapshot:
    return StrategyConfigSnapshot.model_validate(
        {
            "version_id": version_id,
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
            "source_reason": "test",
            "ttl_sec": 300,
        }
    )


def test_redis_key_naming_matches_hot_state_contract() -> None:
    assert RedisKeys.latest_snapshot() == "xuanshu:strategy:latest"
    assert RedisKeys.run_mode() == "xuanshu:runtime:mode"
    assert RedisKeys.symbol_runtime("BTC-USDT-SWAP") == "xuanshu:runtime:symbol:BTC-USDT-SWAP"
    assert RedisKeys.budget_pool_summary() == "xuanshu:runtime:budget_pool"
    assert RedisKeys.governor_health_summary() == "xuanshu:runtime:governor_health"


def test_redis_symbol_runtime_rejects_unsafe_input() -> None:
    with pytest.raises(ValueError):
        RedisKeys.symbol_runtime("btc/usdt swap")


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


def test_redis_runtime_state_store_round_trips_run_mode() -> None:
    store = RedisRuntimeStateStore(redis_client=_FakeRedis())

    store.set_run_mode(RunMode.REDUCE_ONLY)

    assert store.get_run_mode() == RunMode.REDUCE_ONLY


def test_redis_runtime_summary_and_fault_store_round_trips_json() -> None:
    store = RedisRuntimeStateStore(redis_client=_FakeRedis())

    store.set_symbol_runtime_summary(
        "BTC-USDT-SWAP",
        {"symbol": "BTC-USDT-SWAP", "run_mode": "normal", "net_quantity": 1.0},
    )
    store.set_fault_flags({"public_ws_disconnected": {"severity": "warn"}})
    store.set_budget_pool_summary(
        {"remaining_notional": 100.0, "remaining_order_count": 10, "current_mode": "normal"}
    )
    store.set_governor_health_summary({"status": "published", "trigger": "risk_event"})

    assert store.get_symbol_runtime_summary("BTC-USDT-SWAP") == {
        "symbol": "BTC-USDT-SWAP",
        "run_mode": "normal",
        "net_quantity": 1.0,
    }
    assert store.get_fault_flags() == {"public_ws_disconnected": {"severity": "warn"}}
    assert store.get_budget_pool_summary() == {
        "remaining_notional": 100.0,
        "remaining_order_count": 10,
        "current_mode": "normal",
    }
    assert store.get_governor_health_summary() == {"status": "published", "trigger": "risk_event"}


def test_redis_runtime_state_store_ignores_malformed_symbol_summary_json() -> None:
    client = _FakeRedis()
    client.set(RedisKeys.symbol_runtime("BTC-USDT-SWAP"), "{not-json")
    store = RedisRuntimeStateStore(redis_client=client)

    assert store.get_symbol_runtime_summary("BTC-USDT-SWAP") is None


def test_redis_runtime_state_store_ignores_malformed_fault_flags_json() -> None:
    client = _FakeRedis()
    client.set(RedisKeys.fault_flags(), "{not-json")
    store = RedisRuntimeStateStore(redis_client=client)

    assert store.get_fault_flags() is None


def test_redis_runtime_state_store_ignores_malformed_non_utf8_run_mode_bytes() -> None:
    client = _FakeRedis()
    client.values[RedisKeys.run_mode()] = b"\xff\xfe\xfd"
    store = RedisRuntimeStateStore(redis_client=client)

    assert store.get_run_mode() is None


def test_postgres_store_exposes_append_fact_methods() -> None:
    store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")

    assert hasattr(store, "append_order_fact")
    assert hasattr(store, "append_fill_fact")
    assert hasattr(store, "append_position_fact")
    assert hasattr(store, "append_risk_event")
    assert hasattr(store, "save_checkpoint")
    assert hasattr(store, "append_notification_event")
    assert hasattr(store, "list_recent_rows")


@pytest.mark.parametrize(
    ("method_name", "table_name"),
    [
        ("append_order_fact", "orders"),
        ("append_fill_fact", "fills"),
        ("append_position_fact", "positions"),
        ("append_risk_event", "risk_events"),
        ("save_checkpoint", "execution_checkpoints"),
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
        "expert_opinions",
        "governor_runs",
        "notification_events",
    )


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
            "symbol": "BTC-USDT-SWAP",
            "side": "buy",
            "status": "filled",
            "client_order_id": "btc-breakout-000001",
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
        "symbol": "BTC-USDT-SWAP",
        "side": "buy",
        "status": "filled",
        "client_order_id": "btc-breakout-000001",
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
            "symbol": "BTC-USDT-SWAP",
            "side": "buy",
            "status": "filled",
            "client_order_id": "btc-breakout-000001",
        }
    )

    second = PostgresRuntimeStore(dsn=dsn)

    rows = second.list_recent_rows("orders", limit=1)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC-USDT-SWAP"
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


def test_qdrant_collections_are_deterministic_and_immutable() -> None:
    assert QDRANT_COLLECTIONS == (
        "market_case",
        "risk_case",
        "governance_case",
    )


def test_qdrant_case_store_queries_governance_cases_and_normalizes_payloads() -> None:
    seen_calls: list[tuple[str, dict[str, object]]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "result": {
                    "points": [
                        {
                            "payload": {
                                "case_id": "gov-001",
                                "summary": "manual takeover required after repeated recovery failures",
                                "recommended_mode": "halted",
                            }
                        }
                    ]
                }
            }

    class _Client:
        def post(self, url: str, json: dict[str, object]) -> _Response:
            seen_calls.append((url, json))
            return _Response()

    store = QdrantCaseStore(qdrant_url="http://qdrant:6333", client=_Client())

    cases = store.search_governance_cases(
        {
            "trigger_reason": "risk_event",
            "current_run_mode": "degraded",
            "recommended_mode_floor": "halted",
            "active_fault_flags": ["manual_takeover"],
        },
        limit=2,
    )

    assert cases == [
        {
            "case_id": "gov-001",
            "summary": "manual takeover required after repeated recovery failures",
            "recommended_mode": "halted",
        }
    ]
    assert seen_calls == [
        (
            "http://qdrant:6333/collections/governance_case/points/scroll",
            {
                "limit": 2,
                "with_payload": True,
                "with_vectors": False,
                "filter": {
                    "must": [
                        {"key": "trigger_reason", "match": {"value": "risk_event"}},
                        {"key": "current_run_mode", "match": {"value": "degraded"}},
                        {"key": "recommended_mode_floor", "match": {"value": "halted"}},
                        {"key": "active_fault_flags", "match": {"any": ["manual_takeover"]}},
                    ]
                },
            },
        )
    ]

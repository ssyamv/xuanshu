import pytest

from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.infra.storage.postgres_store import POSTGRES_TABLES, PostgresRuntimeStore
from xuanshu.infra.storage.qdrant_store import QDRANT_COLLECTIONS
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

    assert store.get_symbol_runtime_summary("BTC-USDT-SWAP") == {
        "symbol": "BTC-USDT-SWAP",
        "run_mode": "normal",
        "net_quantity": 1.0,
    }
    assert store.get_fault_flags() == {"public_ws_disconnected": {"severity": "warn"}}


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


def test_postgres_store_exposes_append_fact_methods() -> None:
    store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")

    assert hasattr(store, "append_order_fact")
    assert hasattr(store, "append_fill_fact")
    assert hasattr(store, "append_position_fact")
    assert hasattr(store, "append_risk_event")
    assert hasattr(store, "save_checkpoint")


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


def test_qdrant_collections_are_deterministic_and_immutable() -> None:
    assert QDRANT_COLLECTIONS == (
        "market_case",
        "risk_case",
        "governance_case",
    )

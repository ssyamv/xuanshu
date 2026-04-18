import pytest

from xuanshu.infra.storage.postgres_store import POSTGRES_TABLES
from xuanshu.infra.storage.qdrant_store import QDRANT_COLLECTIONS
from xuanshu.infra.storage.redis_store import RedisKeys, RedisSnapshotStore


def test_redis_key_naming_matches_hot_state_contract() -> None:
    assert RedisKeys.latest_snapshot() == "xuanshu:strategy:latest"
    assert RedisKeys.run_mode() == "xuanshu:runtime:mode"
    assert RedisKeys.symbol_runtime("BTC-USDT-SWAP") == "xuanshu:runtime:symbol:BTC-USDT-SWAP"


def test_redis_symbol_runtime_rejects_unsafe_input() -> None:
    with pytest.raises(ValueError):
        RedisKeys.symbol_runtime("btc/usdt swap")


def test_redis_snapshot_store_keeps_only_the_latest_snapshot() -> None:
    store = RedisSnapshotStore()
    first_snapshot = object()
    second_snapshot = object()

    store.set_latest_snapshot("v1", first_snapshot)
    store.set_latest_snapshot("v2", second_snapshot)

    assert store.latest_version_id == "v2"
    assert store.latest_snapshot is second_snapshot


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

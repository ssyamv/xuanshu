import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import xuanshu.apps.trader as trader_app
from xuanshu.contracts.events import (
    AccountSnapshotEvent,
    FaultEvent,
    MarketTradeEvent,
    OrderUpdateEvent,
    OrderbookTopEvent,
    PositionUpdateEvent,
)
from xuanshu.contracts.risk import CandidateSignal
from xuanshu.core.enums import ApprovalState, EntryType, OrderSide, RunMode, SignalUrgency, StrategyId, TraderEventType
from xuanshu.infra.okx.private_ws import OkxPrivateStream
from xuanshu.infra.okx.public_ws import OkxPublicStream
from xuanshu.infra.okx.rest import OkxRestClient
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.infra.storage.redis_store import RedisRuntimeStateStore, RedisSnapshotStore


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def set(self, key: str, value: str) -> bool:
        self.values[key] = value.encode("utf-8")
        return True

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)


class _FakeRestClient:
    def __init__(self) -> None:
        self.placed_orders: list[tuple[dict[str, str], str]] = []
        self.open_orders_calls: list[tuple[str, str]] = []
        self.positions_calls: list[tuple[str, str]] = []

    async def place_order(self, payload: dict[str, str], timestamp: str) -> list[dict[str, object]]:
        self.placed_orders.append((payload, timestamp))
        return [{"ordId": "ord-1", "clOrdId": payload["clOrdId"], "sCode": "0"}]

    async def fetch_open_orders(self, symbol: str, timestamp: str) -> list[dict[str, object]]:
        self.open_orders_calls.append((symbol, timestamp))
        return []

    async def fetch_positions(self, symbol: str, timestamp: str) -> list[dict[str, object]]:
        self.positions_calls.append((symbol, timestamp))
        return []

    async def aclose(self) -> None:
        return None


class _FakeEventStream:
    def __init__(self, events: list[object]) -> None:
        self.events = list(events)
        self.calls: list[dict[str, object]] = []

    def iter_events(self, **kwargs: object):
        self.calls.append(dict(kwargs))

        async def _generator():
            for event in self.events:
                yield event

        return _generator()


def _replace_runtime_streams_with_empty_event_streams(runtime: trader_app.TraderRuntime) -> None:
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=runtime.components.checkpoint_service,
        okx_rest_client=runtime.components.okx_rest_client,
        okx_public_stream=_FakeEventStream([]),
        okx_private_stream=_FakeEventStream([]),
        client_order_id_builder=runtime.components.client_order_id_builder,
    )


def _set_required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XUANSHU_OKX_SYMBOLS", "BTC-USDT-SWAP, ETH-USDT-SWAP")
    monkeypatch.setenv("XUANSHU_TRADER_STARTING_NAV", "250000")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql+psycopg://xuanshu:xuanshu@localhost:5432/xuanshu")
    monkeypatch.setenv("OKX_API_KEY", "api-key")
    monkeypatch.setenv("OKX_API_SECRET", "api-secret")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "api-passphrase")


def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "XUANSHU_OKX_SYMBOLS",
        "XUANSHU_TRADER_STARTING_NAV",
        "OKX_API_KEY",
        "OKX_API_SECRET",
        "OKX_API_PASSPHRASE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_trader_entrypoint_loads_settings_and_threads_it_into_components(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)

    seen_components = None

    async def _noop_wait_forever() -> None:
        return None

    async def fake_run_trader(runtime: trader_app.TraderRuntime) -> None:
        nonlocal seen_components
        seen_components = runtime
        await _noop_wait_forever()

    monkeypatch.setattr(trader_app, "_run_trader", fake_run_trader)

    assert trader_app.main() == 0

    assert seen_components is not None
    assert seen_components.settings.okx_symbols == ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    assert seen_components.starting_nav == 250_000.0
    assert seen_components.components.state_engine.__class__.__name__ == "StateEngine"
    assert seen_components.components.risk_kernel.nav == 250_000.0
    assert seen_components.components.checkpoint_service.__class__.__name__ == "CheckpointService"
    assert isinstance(seen_components.components.okx_rest_client, OkxRestClient)
    assert isinstance(seen_components.components.okx_public_stream, OkxPublicStream)
    assert isinstance(seen_components.components.okx_private_stream, OkxPrivateStream)
    assert seen_components.components.client_order_id_builder("BTC-USDT-SWAP", "breakout", 1) == "BTCUSDTSWAPbreakout000001"
    assert seen_components.settings.okx_api_key.get_secret_value() == "api-key"
    assert seen_components.components.okx_rest_client.api_key == "api-key"
    assert seen_components.components.okx_rest_client.simulated_trading is False
    assert seen_components.history_store.dsn == "postgresql+psycopg://xuanshu:xuanshu@localhost:5432/xuanshu"
    assert seen_components.components.okx_public_stream.url.endswith("/public")
    assert seen_components.components.okx_private_stream.url.endswith("/private")


def test_trader_entrypoint_threads_demo_account_mode_into_okx_clients(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_OKX_ACCOUNT_MODE", "demo")

    seen_runtime = None

    async def fake_run_trader(runtime: trader_app.TraderRuntime) -> None:
        nonlocal seen_runtime
        seen_runtime = runtime
        await runtime.components.okx_rest_client.aclose()

    monkeypatch.setattr(trader_app, "_run_trader", fake_run_trader)

    assert trader_app.main() == 0
    assert seen_runtime is not None
    assert seen_runtime.settings.okx_account_mode.value == "demo"
    assert seen_runtime.components.okx_rest_client.simulated_trading is True
    assert seen_runtime.components.okx_private_stream.simulated_trading is True
    assert seen_runtime.components.okx_public_stream.url == "wss://wspap.okx.com:8443/ws/v5/public"
    assert seen_runtime.components.okx_private_stream.url == "wss://wspap.okx.com:8443/ws/v5/private"


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


def test_trader_entrypoint_loads_runtime_from_temp_dotenv(monkeypatch, tmp_path) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text(
        "\n".join(
            [
                "XUANSHU_OKX_SYMBOLS=BTC-USDT-SWAP,ETH-USDT-SWAP",
                "XUANSHU_TRADER_STARTING_NAV=333333",
                "OKX_API_KEY=api-key",
                "OKX_API_SECRET=api-secret",
                "OKX_API_PASSPHRASE=api-passphrase",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    async def fake_run_trader(runtime: trader_app.TraderRuntime) -> None:
        assert runtime.starting_nav == 333_333.0
        assert runtime.settings.trader_starting_nav == 333_333.0
        await runtime.components.okx_rest_client.aclose()

    monkeypatch.setattr(trader_app, "_run_trader", fake_run_trader)

    assert trader_app.main() == 0


def test_trader_runtime_contract_lists_starting_nav() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "XUANSHU_TRADER_STARTING_NAV=" in env_example
    assert "XUANSHU_TRADER_STARTING_NAV:" in compose


def test_trader_runtime_contract_lists_account_mode() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")
    prod_env_example = Path(".env.prod.example").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "XUANSHU_OKX_ACCOUNT_MODE=" in env_example
    assert "XUANSHU_OKX_ACCOUNT_MODE=" in prod_env_example
    assert "XUANSHU_OKX_ACCOUNT_MODE:" in compose


def test_deployment_contract_lists_fixed_strategy_snapshot_path() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")
    prod_env_example = Path(".env.prod.example").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH=" in env_example
    assert "XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH=" in prod_env_example
    assert "XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH:" in compose


def test_single_host_deploy_contract_lists_prod_env_template() -> None:
    prod_env = Path(".env.prod.example").read_text(encoding="utf-8")

    assert "XUANSHU_ENV=prod" in prod_env
    assert "XUANSHU_DEFAULT_RUN_MODE=halted" in prod_env
    assert "OKX_API_KEY=" in prod_env
    assert "TELEGRAM_BOT_TOKEN=" in prod_env
    assert "OPENAI_API_KEY=" not in prod_env
    assert "QDRANT_URL=" not in prod_env
    assert "XUANSHU_RESEARCH_PROVIDER=" not in prod_env


def test_single_host_deploy_doc_pins_compose_entrypoint() -> None:
    deploy_doc = Path("docs/operations/single-host-deploy.md").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "docker compose --env-file .env.prod up -d --build" in deploy_doc
    assert "restart: unless-stopped" in compose
    assert "XUANSHU_DEFAULT_RUN_MODE:" in compose
    assert "trader:" in compose
    assert "notifier:" in compose
    assert "redis:" in compose
    assert "postgres:" in compose
    assert "governor:" not in compose
    assert "qdrant:" not in compose
    assert "XUANSHU_RESEARCH_PROVIDER:" not in compose
    assert "OPENAI_API_KEY:" not in compose
    assert "QDRANT_URL:" not in compose
    assert "XUANSHU_CODEX_AUTH_DIR" not in compose
    assert "nodejs npm" not in dockerfile
    assert "@openai/codex" not in dockerfile


def test_single_host_operations_docs_describe_fixed_strategy_runtime() -> None:
    alerts = Path("docs/operations/alerts.md").read_text(encoding="utf-8")
    runbook = Path("docs/operations/runbook.md").read_text(encoding="utf-8")
    logging = Path("docs/operations/logging.md").read_text(encoding="utf-8")

    assert "fixed strategy snapshot" in runbook
    assert "XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH" in runbook
    assert "research" not in runbook.lower()
    assert "committee" not in runbook.lower()
    assert "codex" not in runbook.lower()
    assert "governor" not in runbook.lower()
    assert "qdrant" not in alerts.lower()
    assert "research" not in alerts.lower()
    assert "committee" not in alerts.lower()
    assert "governor" not in logging.lower()
    assert "docker compose logs -f trader notifier" in logging


def test_trader_runtime_loads_starting_nav_from_settings(monkeypatch) -> None:
    monkeypatch.setenv("XUANSHU_TRADER_STARTING_NAV", "250000")
    monkeypatch.setenv("OKX_API_KEY", "api-key")
    monkeypatch.setenv("OKX_API_SECRET", "api-secret")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "api-passphrase")
    runtime = trader_app.build_trader_runtime()
    assert runtime.starting_nav == 250000.0


def test_trader_runtime_starts_in_default_run_mode_from_settings(monkeypatch) -> None:
    monkeypatch.setenv("XUANSHU_DEFAULT_RUN_MODE", "halted")
    monkeypatch.setenv("OKX_API_KEY", "api-key")
    monkeypatch.setenv("OKX_API_SECRET", "api-secret")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "api-passphrase")

    runtime = trader_app.build_trader_runtime()

    assert runtime.current_mode == RunMode.HALTED
    assert runtime.startup_snapshot.market_mode == RunMode.HALTED
    assert runtime.startup_checkpoint.current_mode == RunMode.HALTED


def test_trader_runtime_reads_latest_snapshot_from_shared_store(monkeypatch) -> None:
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

    runtime = trader_app.build_trader_runtime()
    runtime.snapshot_store.set_latest_snapshot(
        "snap-shared",
        runtime.startup_snapshot.model_copy(update={"version_id": "snap-shared"}),
    )

    assert runtime.snapshot_store.get_latest_snapshot().version_id == "snap-shared"


def test_trader_runtime_checks_checkpoint_before_waiting(monkeypatch) -> None:
    seen_can_open = []

    async def _noop_wait_forever() -> None:
        return None

    class _CheckpointProbe:
        def can_open_new_risk(self, checkpoint) -> bool:
            seen_can_open.append(checkpoint.needs_reconcile)
            return False

    _set_required_settings_env(monkeypatch)
    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)

    runtime = trader_app.build_trader_runtime()
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=_CheckpointProbe(),
        okx_rest_client=runtime.components.okx_rest_client,
        okx_public_stream=_FakeEventStream([]),
        okx_private_stream=_FakeEventStream([]),
        client_order_id_builder=runtime.components.client_order_id_builder,
    )

    async def _run_and_close_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
        finally:
            await runtime.components.aclose()

    asyncio.run(_run_and_close_runtime())

    assert seen_can_open == [False, False]


def test_trader_runtime_stays_alive_when_startup_gating_blocks_opening(monkeypatch) -> None:
    blocked = []

    async def _noop_wait_forever() -> None:
        blocked.append("waited")
        return None

    class _CheckpointProbe:
        def can_open_new_risk(self, checkpoint) -> bool:
            return False

    _set_required_settings_env(monkeypatch)
    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)

    runtime = trader_app.build_trader_runtime()
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=_CheckpointProbe(),
        okx_rest_client=runtime.components.okx_rest_client,
        okx_public_stream=_FakeEventStream([]),
        okx_private_stream=_FakeEventStream([]),
        client_order_id_builder=runtime.components.client_order_id_builder,
    )

    async def _run_and_close_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
        finally:
            await runtime.components.aclose()

    asyncio.run(_run_and_close_runtime())

    assert blocked == ["waited"]


def test_trader_runtime_applies_snapshot_mode_to_runtime_and_risk(monkeypatch) -> None:
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

    runtime = trader_app.build_trader_runtime()
    _replace_runtime_streams_with_empty_event_streams(runtime)
    runtime.snapshot_store.set_latest_snapshot(
        "snap-halted",
        runtime.startup_snapshot.model_copy(
            update={
                "version_id": "snap-halted",
                "market_mode": RunMode.HALTED,
                "approval_state": ApprovalState.APPROVED,
            }
        ),
    )

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)

    async def _run_and_close_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
        finally:
            await runtime.components.aclose()

    asyncio.run(_run_and_close_runtime())

    decision = runtime.components.risk_kernel.evaluate(
        CandidateSignal(
            symbol="BTC-USDT-SWAP",
            strategy_id=StrategyId.BREAKOUT,
            side=OrderSide.BUY,
            entry_type=EntryType.MARKET,
            urgency=SignalUrgency.HIGH,
            confidence=0.7,
            max_hold_ms=3_000,
            cancel_after_ms=750,
            risk_tag="trend",
        ),
        runtime.startup_snapshot,
    )

    assert runtime.current_mode == RunMode.HALTED
    assert runtime.runtime_store.get_run_mode() == RunMode.HALTED
    assert decision.allow_open is False
    assert "mode_blocks_open" in decision.reason_codes


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
    _replace_runtime_streams_with_empty_event_streams(runtime)
    runtime.snapshot_store.set_latest_snapshot(
        "snap-halted",
        runtime.startup_snapshot.model_copy(
            update={
                "version_id": "snap-halted",
                "market_mode": RunMode.HALTED,
                "approval_state": ApprovalState.APPROVED,
            }
        ),
    )

    async def _exercise_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
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

    summary = runtime.runtime_store.get_symbol_runtime_summary("BTC-USDT-SWAP")
    assert summary["symbol"] == "BTC-USDT-SWAP"
    assert summary["run_mode"] == RunMode.HALTED.value
    assert runtime.components.state_engine.current_run_mode == RunMode.HALTED
    assert runtime.runtime_store.get_run_mode() == RunMode.HALTED


def test_trader_runtime_relaxes_from_halted_when_approved_degraded_snapshot_arrives(monkeypatch) -> None:
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
    _replace_runtime_streams_with_empty_event_streams(runtime)
    runtime.current_mode = RunMode.HALTED
    runtime.components.state_engine.set_run_mode(RunMode.HALTED)
    runtime.snapshot_store.set_latest_snapshot(
        "snap-release",
        runtime.startup_snapshot.model_copy(
            update={
                "version_id": "snap-release",
                "market_mode": RunMode.DEGRADED,
                "approval_state": ApprovalState.APPROVED,
                "symbol_whitelist": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
                "risk_multiplier": 0.25,
                "per_symbol_max_position": 0.12,
            }
        ),
    )

    async def _exercise_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
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

    assert runtime.current_mode == RunMode.DEGRADED
    assert runtime.runtime_store.get_run_mode() == RunMode.DEGRADED


def test_trader_runtime_relaxes_from_halted_at_startup_when_approved_degraded_snapshot_exists(monkeypatch) -> None:
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
    _replace_runtime_streams_with_empty_event_streams(runtime)
    runtime.current_mode = RunMode.HALTED
    runtime.components.state_engine.set_run_mode(RunMode.HALTED)
    runtime.snapshot_store.set_latest_snapshot(
        "snap-release",
        runtime.startup_snapshot.model_copy(
            update={
                "version_id": "snap-release",
                "market_mode": RunMode.DEGRADED,
                "approval_state": ApprovalState.APPROVED,
                "symbol_whitelist": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
                "risk_multiplier": 0.25,
                "per_symbol_max_position": 0.12,
            }
        ),
    )

    async def _run_and_close_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
        finally:
            await runtime.components.aclose()

    asyncio.run(_run_and_close_runtime())

    assert runtime.current_mode == RunMode.DEGRADED
    assert runtime.runtime_store.get_run_mode() == RunMode.DEGRADED


def test_trader_runtime_relaxes_from_reduce_only_at_startup_when_checkpoint_is_clean(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_rest = _FakeRestClient()
    fake_rest._open_orders = []
    fake_rest._positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "pos": "0",
            "markPx": "",
            "upl": "",
        }
    ]

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
    monkeypatch.setattr(trader_app, "build_history_store", lambda settings: history_store)

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)

    runtime = trader_app.build_trader_runtime()
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=runtime.components.checkpoint_service,
        okx_rest_client=fake_rest,
        okx_public_stream=_FakeEventStream([]),
        okx_private_stream=_FakeEventStream([]),
        client_order_id_builder=runtime.components.client_order_id_builder,
    )
    runtime.execution_coordinator = trader_app.ExecutionCoordinator(rest_client=fake_rest)
    runtime.recovery_supervisor = trader_app.RecoverySupervisor(rest_client=fake_rest)

    history_store.save_checkpoint(
        {
            "checkpoint_id": "cp-prev",
            "active_snapshot_version": "snap-old",
            "current_mode": "reduce_only",
            "positions_snapshot": [],
            "open_orders_snapshot": [],
            "budget_state": {
                "max_daily_loss": 100.0,
                "remaining_daily_loss": 100.0,
                "remaining_notional": 100.0,
                "remaining_order_count": 10,
            },
            "last_public_stream_marker": "pub-prev",
            "last_private_stream_marker": "pri-prev",
            "needs_reconcile": False,
        }
    )

    async def _run_and_close_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
        finally:
            await runtime.components.aclose()

    asyncio.run(_run_and_close_runtime())

    assert runtime.startup_snapshot.market_mode == RunMode.NORMAL
    assert runtime.current_mode == RunMode.NORMAL
    assert runtime.runtime_store.get_run_mode() == RunMode.NORMAL


def test_trader_runtime_keeps_snapshot_market_mode_when_recovery_reuses_halted_checkpoint_mode(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_rest = _FakeRestClient()
    fake_rest._open_orders = []
    fake_rest._positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "pos": "0",
            "markPx": "",
            "upl": "",
        }
    ]

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
    monkeypatch.setattr(trader_app, "build_history_store", lambda settings: history_store)

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)

    runtime = trader_app.build_trader_runtime()
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=runtime.components.checkpoint_service,
        okx_rest_client=fake_rest,
        okx_public_stream=_FakeEventStream([]),
        okx_private_stream=_FakeEventStream([]),
        client_order_id_builder=runtime.components.client_order_id_builder,
    )
    runtime.execution_coordinator = trader_app.ExecutionCoordinator(rest_client=fake_rest)
    runtime.recovery_supervisor = trader_app.RecoverySupervisor(rest_client=fake_rest)

    runtime.snapshot_store.set_latest_snapshot(
        "snap-release",
        runtime.startup_snapshot.model_copy(
            update={
                "version_id": "snap-release",
                "market_mode": RunMode.DEGRADED,
                "approval_state": ApprovalState.APPROVED,
            }
        ),
    )
    history_store.save_checkpoint(
        {
            "checkpoint_id": "cp-prev",
            "active_snapshot_version": "snap-old",
            "current_mode": "halted",
            "positions_snapshot": [],
            "open_orders_snapshot": [],
            "budget_state": {
                "max_daily_loss": 100.0,
                "remaining_daily_loss": 100.0,
                "remaining_notional": 100.0,
                "remaining_order_count": 10,
            },
            "last_public_stream_marker": "pub-prev",
            "last_private_stream_marker": "pri-prev",
            "needs_reconcile": False,
        }
    )
    fake_rest.positions_calls.clear()

    async def _run_and_close_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
        finally:
            await runtime.components.aclose()

    asyncio.run(_run_and_close_runtime())

    assert runtime.startup_snapshot.market_mode == RunMode.DEGRADED
    assert runtime.current_mode == RunMode.DEGRADED
    assert runtime.runtime_store.get_run_mode() == RunMode.DEGRADED


def test_trader_runtime_clears_stale_reconcile_flag_after_successful_recovery(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_rest = _FakeRestClient()
    fake_rest._open_orders = []
    fake_rest._positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "pos": "0",
            "markPx": "",
            "upl": "",
        }
    ]

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
    monkeypatch.setattr(trader_app, "build_history_store", lambda settings: history_store)

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)

    runtime = trader_app.build_trader_runtime()
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=runtime.components.checkpoint_service,
        okx_rest_client=fake_rest,
        okx_public_stream=_FakeEventStream([]),
        okx_private_stream=_FakeEventStream([]),
        client_order_id_builder=runtime.components.client_order_id_builder,
    )
    runtime.execution_coordinator = trader_app.ExecutionCoordinator(rest_client=fake_rest)
    runtime.recovery_supervisor = trader_app.RecoverySupervisor(rest_client=fake_rest)
    runtime.snapshot_store.set_latest_snapshot(
        "snap-release",
        runtime.startup_snapshot.model_copy(
            update={
                "version_id": "snap-release",
                "market_mode": RunMode.DEGRADED,
                "approval_state": ApprovalState.APPROVED,
            }
        ),
    )
    history_store.save_checkpoint(
        {
            "checkpoint_id": "cp-prev",
            "active_snapshot_version": "snap-old",
            "current_mode": "halted",
            "positions_snapshot": [],
            "open_orders_snapshot": [],
            "budget_state": {
                "max_daily_loss": 100.0,
                "remaining_daily_loss": 100.0,
                "remaining_notional": 100.0,
                "remaining_order_count": 10,
            },
            "last_public_stream_marker": "pub-prev",
            "last_private_stream_marker": "pri-prev",
            "needs_reconcile": True,
        }
    )

    async def _run_and_close_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
        finally:
            await runtime.components.aclose()

    asyncio.run(_run_and_close_runtime())

    assert runtime.startup_checkpoint.needs_reconcile is False
    assert runtime.current_mode == RunMode.DEGRADED


def test_trader_runtime_does_not_record_account_snapshot_as_risk_event(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")

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
    monkeypatch.setattr(trader_app, "build_history_store", lambda settings: history_store)

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)
    runtime = trader_app.build_trader_runtime()
    _replace_runtime_streams_with_empty_event_streams(runtime)

    async def _exercise_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
            await trader_app._dispatch_runtime_event(
                runtime,
                AccountSnapshotEvent(
                    event_type=TraderEventType.ACCOUNT_SNAPSHOT,
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    private_sequence="pri-1",
                    equity=918.27,
                    available_balance=800.0,
                    margin_ratio=0.15,
                ),
            )
        finally:
            await runtime.components.aclose()

    asyncio.run(_exercise_runtime())

    assert history_store.list_recent_rows("risk_events", limit=5) == []


def test_trader_runtime_dispatches_fault_event_updates_fault_flags(monkeypatch) -> None:
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
    _replace_runtime_streams_with_empty_event_streams(runtime)

    async def _exercise_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
            await trader_app._dispatch_runtime_event(
                runtime,
                FaultEvent(
                    event_type=TraderEventType.RUNTIME_FAULT,
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    severity="warn",
                    code="public_ws_disconnected",
                    detail="public stream dropped",
                ),
            )
        finally:
            await runtime.components.aclose()

    asyncio.run(_exercise_runtime())

    assert runtime.components.state_engine.fault_flags["public_ws_disconnected"] == {
        "severity": "warn",
        "detail": "public stream dropped",
    }
    assert runtime.runtime_store.get_fault_flags() == {
        "public_ws_disconnected": {
            "severity": "warn",
            "detail": "public stream dropped",
        }
    }
    assert runtime.runtime_store.get_run_mode() == RunMode.DEGRADED


def test_trader_runtime_rejects_unsupported_dispatch_event(monkeypatch) -> None:
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
    _replace_runtime_streams_with_empty_event_streams(runtime)

    class _UnsupportedEvent:
        pass

    async def _exercise_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
            with pytest.raises(ValueError, match="unsupported event type: _UnsupportedEvent"):
                await trader_app._dispatch_runtime_event(runtime, _UnsupportedEvent())
        finally:
            await runtime.components.aclose()

    asyncio.run(_exercise_runtime())

    assert runtime.runtime_store.get_run_mode() == RunMode.NORMAL
    assert runtime.runtime_store.get_fault_flags() == {}


def test_trader_runtime_records_recovery_mode_change_for_notifier(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
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
    monkeypatch.setattr(trader_app, "build_history_store", lambda settings: history_store)

    class _CheckpointProbe:
        def can_open_new_risk(self, checkpoint) -> bool:
            return False

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)

    runtime = trader_app.build_trader_runtime()
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=_CheckpointProbe(),
        okx_rest_client=runtime.components.okx_rest_client,
        okx_public_stream=_FakeEventStream([]),
        okx_private_stream=_FakeEventStream([]),
        client_order_id_builder=runtime.components.client_order_id_builder,
    )

    async def _run_and_close_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
        finally:
            await runtime.components.aclose()

    asyncio.run(_run_and_close_runtime())

    assert len(history_store.written_rows["execution_checkpoints"]) == 1
    saved_checkpoint = history_store.written_rows["execution_checkpoints"][0]
    assert saved_checkpoint["checkpoint_id"] == "startup"
    assert saved_checkpoint["current_mode"] == "reduce_only"
    assert saved_checkpoint["needs_reconcile"] is False
    assert history_store.written_rows["risk_events"] == [
        {
            "event_type": "runtime_mode_changed",
            "symbol": "system",
            "detail": "startup gating tightened runtime to reduce_only",
        }
    ]
    budget_summary = runtime.runtime_store.get_budget_pool_summary()
    assert budget_summary["max_daily_loss"] == 100.0
    assert budget_summary["remaining_daily_loss"] == 100.0
    assert budget_summary["remaining_notional"] == 100.0
    assert budget_summary["remaining_order_count"] == 10
    assert budget_summary["current_mode"] == "reduce_only"
    assert budget_summary["starting_nav"] == 250000.0


def test_trader_runtime_runs_startup_recovery_against_persisted_checkpoint(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_rest = _FakeRestClient()
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
    monkeypatch.setattr(trader_app, "build_history_store", lambda settings: history_store)

    runtime = trader_app.build_trader_runtime()
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=runtime.components.checkpoint_service,
        okx_rest_client=fake_rest,
        okx_public_stream=_FakeEventStream([]),
        okx_private_stream=_FakeEventStream([]),
        client_order_id_builder=runtime.components.client_order_id_builder,
    )
    runtime.execution_coordinator = trader_app.ExecutionCoordinator(rest_client=fake_rest)
    runtime.recovery_supervisor = trader_app.RecoverySupervisor(rest_client=fake_rest)
    history_store.save_checkpoint(
        {
            "checkpoint_id": "cp-prev",
            "active_snapshot_version": runtime.startup_snapshot.version_id,
            "current_mode": "normal",
            "positions_snapshot": [],
            "open_orders_snapshot": [],
            "budget_state": {
                "max_daily_loss": 100.0,
                "remaining_daily_loss": 80.0,
                "remaining_notional": 60.0,
                "remaining_order_count": 10,
            },
            "last_public_stream_marker": "pub-prev",
            "last_private_stream_marker": "pri-prev",
            "needs_reconcile": False,
        }
    )

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)

    async def _run_and_close_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
        finally:
            await runtime.components.aclose()

    asyncio.run(_run_and_close_runtime())

    assert [symbol for symbol, _ in fake_rest.open_orders_calls] == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    assert [symbol for symbol, _ in fake_rest.positions_calls] == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    assert len({timestamp for _, timestamp in fake_rest.open_orders_calls}) == 1
    assert len({timestamp for _, timestamp in fake_rest.positions_calls}) == 1


def test_trader_runtime_consumes_public_and_private_streams_and_persists_runtime_facts(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_rest = _FakeRestClient()
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
    monkeypatch.setattr(trader_app, "build_history_store", lambda settings: history_store)

    runtime = trader_app.build_trader_runtime()
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=runtime.components.checkpoint_service,
        okx_rest_client=fake_rest,
        okx_public_stream=_FakeEventStream(
            [
                OrderbookTopEvent(
                    event_type=TraderEventType.ORDERBOOK_TOP,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-1",
                    bid_price=100.0,
                    ask_price=100.2,
                    bid_size=5.0,
                    ask_size=6.0,
                ),
                MarketTradeEvent(
                    event_type=TraderEventType.MARKET_TRADE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-2",
                    price=100.3,
                    size=1.0,
                    side="buy",
                ),
            ]
        ),
        okx_private_stream=_FakeEventStream(
            [
                AccountSnapshotEvent(
                    event_type=TraderEventType.ACCOUNT_SNAPSHOT,
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    private_sequence="pri-1",
                    equity=10_000.0,
                    available_balance=8_000.0,
                    margin_ratio=0.2,
                ),
                OrderUpdateEvent(
                    event_type=TraderEventType.ORDER_UPDATE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    private_sequence="pri-2",
                    order_id="ord-1",
                    client_order_id="BTCUSDTSWAPbreakout000001",
                    side="buy",
                    price=100.2,
                    size=1.0,
                    filled_size=0.0,
                    status="live",
                ),
                PositionUpdateEvent(
                    event_type=TraderEventType.POSITION_UPDATE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    private_sequence="pri-3",
                    net_quantity=1.0,
                    average_price=100.2,
                    mark_price=100.4,
                    unrealized_pnl=0.2,
                ),
            ]
        ),
        client_order_id_builder=runtime.components.client_order_id_builder,
    )
    runtime.execution_coordinator = trader_app.ExecutionCoordinator(rest_client=fake_rest)
    runtime.snapshot_store.set_latest_snapshot(
        "snap-live",
        runtime.startup_snapshot.model_copy(
            update={
                "version_id": "snap-live",
                "market_mode": RunMode.NORMAL,
                "approval_state": ApprovalState.APPROVED,
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
            }
        ),
    )

    async def _run_and_close_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
        finally:
            await runtime.components.aclose()

    asyncio.run(_run_and_close_runtime())

    assert len(fake_rest.placed_orders) == 1
    assert history_store.written_rows["orders"]
    assert history_store.written_rows["positions"]
    assert runtime.runtime_store.get_symbol_runtime_summary("BTC-USDT-SWAP")["net_quantity"] == 1.0
    assert runtime.runtime_store.get_budget_pool_summary()["equity"] == 10_000.0


def test_trader_runtime_does_not_submit_orders_before_non_bootstrap_snapshot_exists(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()
    fake_rest = _FakeRestClient()

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
    _replace_runtime_streams_with_empty_event_streams(runtime)
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=runtime.components.checkpoint_service,
        okx_rest_client=fake_rest,
        okx_public_stream=runtime.components.okx_public_stream,
        okx_private_stream=runtime.components.okx_private_stream,
        client_order_id_builder=runtime.components.client_order_id_builder,
    )
    runtime.execution_coordinator = trader_app.ExecutionCoordinator(rest_client=fake_rest)

    async def _exercise_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
            await trader_app._dispatch_runtime_event(
                runtime,
                OrderbookTopEvent(
                    event_type=TraderEventType.ORDERBOOK_TOP,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-1",
                    bid_price=100.0,
                    ask_price=100.2,
                    bid_size=5.0,
                    ask_size=6.0,
                ),
            )
            await trader_app._dispatch_runtime_event(
                runtime,
                MarketTradeEvent(
                    event_type=TraderEventType.MARKET_TRADE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-2",
                    price=100.3,
                    size=5.0,
                    side="buy",
                ),
            )
            await trader_app._dispatch_runtime_event(
                runtime,
                MarketTradeEvent(
                    event_type=TraderEventType.MARKET_TRADE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-3",
                    price=100.4,
                    size=4.0,
                    side="buy",
                ),
            )
        finally:
            await runtime.components.aclose()

    asyncio.run(_exercise_runtime())

    assert runtime.startup_snapshot.version_id == "bootstrap"
    assert fake_rest.placed_orders == []


def test_trader_runtime_keeps_public_stream_healthy_when_order_submission_fails(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _RejectingRestClient(_FakeRestClient):
        async def place_order(self, payload: dict[str, str], timestamp: str) -> list[dict[str, object]]:
            raise RuntimeError("OKX business error 1: All operations failed")

    fake_rest = _RejectingRestClient()

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
    _replace_runtime_streams_with_empty_event_streams(runtime)
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=runtime.components.checkpoint_service,
        okx_rest_client=fake_rest,
        okx_public_stream=runtime.components.okx_public_stream,
        okx_private_stream=runtime.components.okx_private_stream,
        client_order_id_builder=runtime.components.client_order_id_builder,
    )
    runtime.execution_coordinator = trader_app.ExecutionCoordinator(rest_client=fake_rest)
    runtime.snapshot_store.set_latest_snapshot(
        "snap-live",
        runtime.startup_snapshot.model_copy(
            update={
                "version_id": "snap-live",
                "market_mode": RunMode.NORMAL,
                "approval_state": ApprovalState.APPROVED,
                "symbol_whitelist": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
            }
        ),
    )

    async def _exercise_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
            await trader_app._dispatch_runtime_event(
                runtime,
                OrderbookTopEvent(
                    event_type=TraderEventType.ORDERBOOK_TOP,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-1",
                    bid_price=100.0,
                    ask_price=100.2,
                    bid_size=5.0,
                    ask_size=6.0,
                ),
            )
            await trader_app._dispatch_runtime_event(
                runtime,
                MarketTradeEvent(
                    event_type=TraderEventType.MARKET_TRADE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-2",
                    price=100.3,
                    size=5.0,
                    side="buy",
                ),
            )
            await trader_app._dispatch_runtime_event(
                runtime,
                MarketTradeEvent(
                    event_type=TraderEventType.MARKET_TRADE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-3",
                    price=100.4,
                    size=4.0,
                    side="buy",
                ),
            )
        finally:
            await runtime.components.aclose()

    asyncio.run(_exercise_runtime())

    assert runtime.runtime_store.get_run_mode() == RunMode.NORMAL
    assert runtime.runtime_store.get_fault_flags() == {}


def test_trader_runtime_logs_order_submission(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()
    fake_rest = _FakeRestClient()
    logged: list[tuple[str, dict[str, object]]] = []

    class _Logger:
        def info(self, event: str, *, extra: dict[str, object]) -> None:
            logged.append((event, extra))

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
    monkeypatch.setattr(trader_app, "_LOGGER", _Logger())

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)

    runtime = trader_app.build_trader_runtime()
    _replace_runtime_streams_with_empty_event_streams(runtime)
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=runtime.components.checkpoint_service,
        okx_rest_client=fake_rest,
        okx_public_stream=runtime.components.okx_public_stream,
        okx_private_stream=runtime.components.okx_private_stream,
        client_order_id_builder=runtime.components.client_order_id_builder,
    )
    runtime.execution_coordinator = trader_app.ExecutionCoordinator(rest_client=fake_rest)
    runtime.snapshot_store.set_latest_snapshot(
        "snap-live",
        runtime.startup_snapshot.model_copy(
            update={
                "version_id": "snap-live",
                "market_mode": RunMode.NORMAL,
                "approval_state": ApprovalState.APPROVED,
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
            }
        ),
    )

    async def _exercise_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
            await trader_app._dispatch_runtime_event(
                runtime,
                OrderbookTopEvent(
                    event_type=TraderEventType.ORDERBOOK_TOP,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-1",
                    bid_price=100.0,
                    ask_price=100.2,
                    bid_size=5.0,
                    ask_size=6.0,
                ),
            )
            await trader_app._dispatch_runtime_event(
                runtime,
                MarketTradeEvent(
                    event_type=TraderEventType.MARKET_TRADE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-2",
                    price=100.3,
                    size=5.0,
                    side="buy",
                ),
            )
            await trader_app._dispatch_runtime_event(
                runtime,
                MarketTradeEvent(
                    event_type=TraderEventType.MARKET_TRADE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-3",
                    price=100.4,
                    size=4.0,
                    side="buy",
                ),
            )
        finally:
            await runtime.components.aclose()

    asyncio.run(_exercise_runtime())

    assert len(fake_rest.placed_orders) == 1
    assert logged[-1] == (
        "order_submitted",
        {
            "service": "trader",
            "symbol": "BTC-USDT-SWAP",
            "strategy_id": "breakout",
            "side": "buy",
            "client_order_id": "BTCUSDTSWAPbreakout000001",
            "run_mode": "normal",
        },
    )


def test_trader_runtime_records_trade_notification_metadata_for_order_and_position_events(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()
    fake_rest = _FakeRestClient()

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
    _replace_runtime_streams_with_empty_event_streams(runtime)
    runtime.components = trader_app.TraderComponents(
        state_engine=runtime.components.state_engine,
        risk_kernel=runtime.components.risk_kernel,
        checkpoint_service=runtime.components.checkpoint_service,
        okx_rest_client=fake_rest,
        okx_public_stream=runtime.components.okx_public_stream,
        okx_private_stream=runtime.components.okx_private_stream,
        client_order_id_builder=runtime.components.client_order_id_builder,
    )
    runtime.execution_coordinator = trader_app.ExecutionCoordinator(rest_client=fake_rest)
    runtime.snapshot_store.set_latest_snapshot(
        "snap-live",
        runtime.startup_snapshot.model_copy(
            update={
                "version_id": "snap-live",
                "market_mode": RunMode.NORMAL,
                "approval_state": ApprovalState.APPROVED,
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
            }
        ),
    )

    async def _exercise_runtime() -> None:
        try:
            await trader_app._run_trader(runtime)
            await trader_app._dispatch_runtime_event(
                runtime,
                OrderbookTopEvent(
                    event_type=TraderEventType.ORDERBOOK_TOP,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-1",
                    bid_price=100.0,
                    ask_price=100.2,
                    bid_size=5.0,
                    ask_size=6.0,
                ),
            )
            await trader_app._dispatch_runtime_event(
                runtime,
                MarketTradeEvent(
                    event_type=TraderEventType.MARKET_TRADE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-2",
                    price=100.3,
                    size=5.0,
                    side="buy",
                ),
            )
            await trader_app._dispatch_runtime_event(
                runtime,
                MarketTradeEvent(
                    event_type=TraderEventType.MARKET_TRADE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    public_sequence="pub-3",
                    price=100.4,
                    size=4.0,
                    side="buy",
                ),
            )
            await trader_app._dispatch_runtime_event(
                runtime,
                OrderUpdateEvent(
                    event_type=TraderEventType.ORDER_UPDATE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    private_sequence="pri-1",
                    order_id="ord-1",
                    client_order_id="BTCUSDTSWAPbreakout000001",
                    side="buy",
                    price=100.4,
                    size=1.0,
                    filled_size=1.0,
                    status="filled",
                ),
            )
            await trader_app._dispatch_runtime_event(
                runtime,
                PositionUpdateEvent(
                    event_type=TraderEventType.POSITION_UPDATE,
                    symbol="BTC-USDT-SWAP",
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    private_sequence="pri-2",
                    net_quantity=1.0,
                    average_price=100.4,
                    mark_price=100.5,
                    unrealized_pnl=0.2,
                ),
            )
        finally:
            await runtime.components.aclose()

    asyncio.run(_exercise_runtime())

    assert runtime.history_store.written_rows["orders"][0] == {
        "symbol": "BTC-USDT-SWAP",
        "side": "buy",
        "status": "submitted",
        "client_order_id": "BTCUSDTSWAPbreakout000001",
        "order_id": "ord-1",
        "intent": "open",
        "strategy_id": "breakout",
        "strategy_logic": "趋势突破，最近成交偏买方，准备顺势开多。",
    }
    assert runtime.history_store.written_rows["positions"][-1] == {
        "symbol": "BTC-USDT-SWAP",
        "net_quantity": 1.0,
        "average_price": 100.4,
        "mark_price": 100.5,
        "unrealized_pnl": 0.2,
        "intent": "open",
        "strategy_id": "breakout",
        "strategy_logic": "趋势突破，最近成交偏买方，准备顺势开多。",
    }


def test_trader_entrypoint_fails_fast_without_required_settings(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_TRADER_STARTING_NAV", "0")

    async def unexpected_run_trader(_: trader_app.TraderRuntime) -> None:
        raise AssertionError("trader runtime should not start when settings are invalid")

    monkeypatch.setattr(trader_app, "_run_trader", unexpected_run_trader)

    with pytest.raises(ValidationError):
        trader_app.main()


def test_trader_entrypoint_fails_fast_without_okx_credentials(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("OKX_API_KEY", "")

    async def unexpected_run_trader(_: trader_app.TraderRuntime) -> None:
        raise AssertionError("trader runtime should not start when OKX credentials are invalid")

    monkeypatch.setattr(trader_app, "_run_trader", unexpected_run_trader)

    with pytest.raises(ValidationError):
        trader_app.main()

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import xuanshu.apps.governor as governor_app
from xuanshu.contracts.approval import ApprovalDecision
from xuanshu.contracts.backtest import (
    BacktestDatasetRange,
    BacktestReport,
    OverfitRisk,
    RegimeFit,
    TradeCountSufficiency,
)
from xuanshu.contracts.research import ResearchTrigger, StrategyPackage
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.governor.research import StrategyResearchEngine
import xuanshu.governor.research_providers as research_providers_module
from xuanshu.governor.research_providers import ResearchProviderName
from xuanshu.infra.ai.governor_client import (
    CodexCliGovernorAgentRunner,
    ConfiguredGovernorAgentRunner,
    GovernorClient,
)
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.infra.storage.redis_store import RedisSnapshotStore


def _sample_strategy_definition(
    *,
    strategy_family: str = "breakout",
    directionality: str = "long_only",
    parameter_set: dict[str, object] | None = None,
    score: float = 67.5,
) -> dict[str, object]:
    if parameter_set is None:
        parameter_set = {
            "lookback": 20,
            "signal_mode": "breakout_confirmed",
            "stop_loss_bps": 50,
            "take_profit_bps": 120,
            "risk_fraction": 0.0025,
            "max_hold_minutes": 60,
        }
    return {
        "strategy_def_id": "strat-app-001",
        "symbol": "BTC-USDT-SWAP",
        "strategy_family": strategy_family,
        "directionality": directionality,
        "feature_spec": {"indicators": [{"name": "sma", "source": "close", "window": 20}]},
        "entry_rules": {"all": [{"op": "crosses_above", "left": "close", "right": "sma_20"}]},
        "exit_rules": {
            "any": [
                {"op": "crosses_below", "left": "close", "right": "sma_20"},
                {"op": "take_profit_bps", "value": 120},
                {"op": "stop_loss_bps", "value": 50},
                {"op": "time_stop_minutes", "value": 60},
            ]
        },
        "position_sizing_rules": {"risk_fraction": 0.0025},
        "risk_constraints": {"max_hold_minutes": 60},
        "parameter_set": parameter_set,
        "score": score,
        "score_basis": "backtest_return_percent",
    }


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def set(self, key: str, value: str) -> bool:
        self.values[key] = value.encode("utf-8")
        return True

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    def delete(self, key: str) -> int:
        existed = key in self.values
        self.values.pop(key, None)
        return 1 if existed else 0


class _FakeMarketDataClient:
    def __init__(self, candles: list[dict[str, object]]) -> None:
        self.candles = candles

    async def fetch_history_candles(
        self,
        symbol: str,
        *,
        bar: str = "1H",
        after: str | None = None,
        before: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        return self.candles


def _build_market_candles(
    closes: list[float],
    *,
    end: datetime | None = None,
) -> list[dict[str, object]]:
    latest = end or datetime.now(UTC)
    earliest = latest - timedelta(days=180, hours=1)
    if len(closes) == 1:
        timestamps = [earliest]
    else:
        step = (latest - earliest) / (len(closes) - 1)
        timestamps = [earliest + step * index for index in range(len(closes))]
    candles = [
        {
            "ts": str(int(timestamp.timestamp() * 1000)),
            "open": str(close),
            "high": str(close),
            "low": str(close),
            "close": str(close),
        }
        for timestamp, close in zip(timestamps, closes, strict=True)
    ]
    return list(reversed(candles))


def _stub_market_data_client(
    monkeypatch: pytest.MonkeyPatch,
    candles: list[dict[str, object]],
) -> None:
    monkeypatch.setattr(
        governor_app,
        "build_market_data_client",
        lambda settings: _FakeMarketDataClient(candles),
    )


def _set_required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql+psycopg://xuanshu:xuanshu@localhost:5432/xuanshu")


def _clear_unrelated_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REDIS_URL",
        "QDRANT_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "OKX_API_KEY",
        "OKX_API_SECRET",
        "OKX_API_PASSPHRASE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_governor_entrypoint_loads_settings_and_threads_it_into_runtime(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)

    seen_runtime = None

    async def _noop_wait_forever() -> None:
        return None

    async def fake_run_governor(runtime: governor_app.GovernorRuntime) -> None:
        nonlocal seen_runtime
        seen_runtime = runtime
        await _noop_wait_forever()

    monkeypatch.setattr(governor_app, "_run_governor", fake_run_governor)

    assert governor_app.main() == 0

    assert seen_runtime is not None
    assert seen_runtime.service.__class__.__name__ == "GovernorService"
    assert seen_runtime.settings.openai_api_key.get_secret_value() == "openai-key"
    assert isinstance(seen_runtime.governor_client, GovernorClient)
    assert isinstance(seen_runtime.governor_client.agent_runner, ConfiguredGovernorAgentRunner)
    assert seen_runtime.governor_client.agent_runner.api_key.get_secret_value() == "openai-key"
    assert seen_runtime.governor_client.agent_runner.timeout_sec == 12
    assert seen_runtime.settings.research_provider == ResearchProviderName.API
    assert seen_runtime.history_store.dsn == "postgresql+psycopg://xuanshu:xuanshu@localhost:5432/xuanshu"
    assert seen_runtime.last_snapshot.version_id == "bootstrap"


def test_governor_entrypoint_fails_fast_without_required_settings(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    _clear_unrelated_settings_env(monkeypatch)

    async def unexpected_run_governor(_: governor_app.GovernorRuntime) -> None:
        raise AssertionError("governor runtime should not start when settings are invalid")

    monkeypatch.setattr(governor_app, "_run_governor", unexpected_run_governor)

    with pytest.raises(ValidationError):
        governor_app.main()


def test_governor_entrypoint_supports_codex_cli_research_provider(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_RESEARCH_PROVIDER", "codex_cli")

    seen_runtime = None

    async def fake_run_governor(runtime: governor_app.GovernorRuntime) -> None:
        nonlocal seen_runtime
        seen_runtime = runtime

    monkeypatch.setattr(governor_app, "_run_governor", fake_run_governor)

    assert governor_app.main() == 0

    assert seen_runtime is not None
    assert seen_runtime.settings.research_provider == ResearchProviderName.CODEX_CLI
    assert seen_runtime.research_engine.provider.provider_name == ResearchProviderName.CODEX_CLI


def test_governor_entrypoint_supports_codex_cli_without_openai_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql+psycopg://xuanshu:xuanshu@localhost:5432/xuanshu")
    _clear_unrelated_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_RESEARCH_PROVIDER", "codex_cli")

    seen_runtime = None

    async def fake_run_governor(runtime: governor_app.GovernorRuntime) -> None:
        nonlocal seen_runtime
        seen_runtime = runtime

    monkeypatch.setattr(governor_app, "_run_governor", fake_run_governor)

    assert governor_app.main() == 0

    assert seen_runtime is not None
    assert seen_runtime.settings.openai_api_key is None
    assert isinstance(seen_runtime.governor_client.agent_runner, CodexCliGovernorAgentRunner)


def test_governor_entrypoint_rejects_unsupported_research_provider(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_RESEARCH_PROVIDER", "chatgpt_pro_web")

    async def unexpected_run_governor(_: governor_app.GovernorRuntime) -> None:
        raise AssertionError("governor runtime should not start when research provider is invalid")

    monkeypatch.setattr(governor_app, "_run_governor", unexpected_run_governor)

    with pytest.raises(ValidationError):
        governor_app.main()


def test_governor_research_bridge_stays_idle_without_real_historical_rows(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    _stub_market_data_client(monkeypatch, [])

    runtime = governor_app.build_governor_runtime()

    assert (
        asyncio.run(
            governor_app._build_research_candidates(
                runtime,
                {"symbol_summaries": [{"symbol": "BTC-USDT-SWAP"}]},
            )
        )
        == []
    )


def test_governor_research_bridge_builds_candidates_from_store_backed_position_rows(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    _stub_market_data_client(monkeypatch, _build_market_candles([100.0, 101.0, 102.0, 103.0, 104.0]))

    runtime = governor_app.build_governor_runtime()

    class _Provider:
        provider_name = ResearchProviderName.API

        async def generate_analysis(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return research_providers_module.ResearchProviderSuggestion(
                thesis="trend remains constructive",
                strategy_family="breakout",
                entry_signal="provider_breakout_signal",
                exit_stop_loss_bps=50,
                exit_take_profit_bps=120,
                risk_fraction=0.0025,
                max_hold_minutes=60,
                failure_modes=[],
                invalidating_conditions=[],
            )

    runtime.research_engine = StrategyResearchEngine(provider=_Provider())

    runtime.history_store.append_order_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "generated_at": "2026-04-19T00:00:00Z",
            "price": 100.0,
        }
    )
    runtime.history_store.append_fill_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "generated_at": "2026-04-19T00:03:00Z",
            "price": 101.5,
        }
    )
    runtime.history_store.append_position_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "generated_at": "2026-04-19T00:05:00Z",
            "mark_price": 100.0,
        }
    )
    runtime.history_store.append_position_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "mark_price": 103.0,
            "generated_at": "2026-04-19T00:07:00Z",
        }
    )

    candidates = asyncio.run(
        governor_app._build_research_candidates(
            runtime,
            {"symbol_summaries": [{"symbol": "BTC-USDT-SWAP"}]},
        )
    )

    assert len(candidates) >= 3
    first_candidate = candidates[0]
    assert first_candidate.symbol_scope == ["BTC-USDT-SWAP"]
    assert first_candidate.backtest_summary == {
        "row_count": 4,
        "start_close": 101.0,
        "end_close": 104.0,
        "close_change_bps": 297.029703,
    }
    assert {tuple(candidate.symbol_scope) for candidate in candidates} == {("BTC-USDT-SWAP",)}
    assert {candidate.market_environment_scope[0] for candidate in candidates} >= {"trend", "range", "mean_reversion"}
    assert "all" in first_candidate.entry_rules


def test_governor_research_bridge_builds_candidates_for_each_symbol_scope(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    btc_candles = _build_market_candles([100.0, 101.0, 102.0, 103.0, 104.0])
    eth_candles = _build_market_candles([200.0, 201.0, 202.0, 203.0, 204.0])

    class _MarketDataClient:
        async def fetch_history_candles(self, symbol: str, **_: object) -> list[dict[str, object]]:
            return btc_candles if symbol == "BTC-USDT-SWAP" else eth_candles

    class _Provider:
        provider_name = ResearchProviderName.CODEX_CLI

        async def generate_analyses(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return [
                research_providers_module.ResearchProviderSuggestion(
                    thesis=f"{symbol_scope[0]} {market_environment}",
                    strategy_family="breakout",
                    entry_signal="breakout_confirmed",
                    exit_stop_loss_bps=50,
                    exit_take_profit_bps=120,
                    risk_fraction=0.0025,
                    max_hold_minutes=60,
                    failure_modes=[],
                    invalidating_conditions=[],
                )
            ]

    monkeypatch.setattr(governor_app, "build_market_data_client", lambda settings: _MarketDataClient())
    monkeypatch.setattr(
        governor_app,
        "build_research_engine",
        lambda settings: StrategyResearchEngine(provider=_Provider()),
    )

    runtime = governor_app.build_governor_runtime()
    result = asyncio.run(
        governor_app._prepare_research_candidates(
            runtime,
            {
                "symbol_summaries": [
                    {"symbol": "BTC-USDT-SWAP"},
                    {"symbol": "ETH-USDT-SWAP"},
                ]
            },
        )
    )

    assert result.status == "candidate_built"
    assert {tuple(candidate.symbol_scope) for candidate in result.candidates} == {
        ("BTC-USDT-SWAP",),
        ("ETH-USDT-SWAP",),
    }


def test_governor_research_bridge_builds_candidates_across_market_environments(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    _stub_market_data_client(monkeypatch, _build_market_candles([100.0, 101.0, 102.0, 103.0, 104.0]))
    calls: list[tuple[tuple[str, ...], str]] = []

    class _Provider:
        provider_name = ResearchProviderName.CODEX_CLI

        async def generate_analyses(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            calls.append((tuple(symbol_scope), market_environment))
            return [
                research_providers_module.ResearchProviderSuggestion(
                    thesis=f"{market_environment} thesis",
                    strategy_family="breakout" if market_environment == "trend" else "mean_reversion",
                    entry_signal="breakout_confirmed" if market_environment == "trend" else "range_retest",
                    exit_stop_loss_bps=50,
                    exit_take_profit_bps=120,
                    risk_fraction=0.0025,
                    max_hold_minutes=60,
                    failure_modes=[],
                    invalidating_conditions=[],
                )
            ]

    monkeypatch.setattr(
        governor_app,
        "build_research_engine",
        lambda settings: StrategyResearchEngine(provider=_Provider()),
    )

    runtime = governor_app.build_governor_runtime()
    result = asyncio.run(
        governor_app._prepare_research_candidates(
            runtime,
            {"symbol_summaries": [{"symbol": "BTC-USDT-SWAP"}]},
        )
    )

    assert result.status == "candidate_built"
    assert {market_environment for _scope, market_environment in calls} >= {"trend", "range", "mean_reversion"}


def test_governor_runtime_runs_one_cycle_and_publishes_snapshot(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    captured_state_summary = None

    class _Runner:
        async def run(self, state_summary):
            nonlocal captured_state_summary
            captured_state_summary = state_summary
            now = datetime.now(UTC)
            return {
                "version_id": "snap-new",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    monkeypatch.setattr(
        governor_app,
        "build_governor_client",
        lambda settings: GovernorClient(_Runner()),
    )

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_run_mode(RunMode.DEGRADED)
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert captured_state_summary["scope"] == "governor"
    assert captured_state_summary["current_run_mode"] == "degraded"
    assert captured_state_summary["latest_snapshot_version"] == "bootstrap"
    assert captured_state_summary["active_fault_flags"] == []
    assert captured_state_summary["symbol_summaries"] == []
    assert captured_state_summary["recent_risk_events"] == []
    assert captured_state_summary["recent_governor_runs"] == []
    assert captured_state_summary["trigger_reason"] == "mode_change"
    assert captured_state_summary["committee_summary"] == {
        "consensus_decision": "maintain",
        "recommended_mode_floor": "normal",
        "blocking_flags": [],
        "requires_human_review": False,
        "active_experts": ["market_structure", "risk", "event_filter"],
    }
    assert [opinion["expert_type"] for opinion in captured_state_summary["expert_opinions"]] == [
        "market_structure",
        "risk",
        "event_filter",
    ]
    assert runtime.last_snapshot.version_id.startswith("governor-")
    assert [snapshot.version_id for snapshot in runtime.published_snapshots] == [runtime.last_snapshot.version_id]


def test_governor_runtime_observes_configured_symbol_scope_instead_of_stale_snapshot_whitelist(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    captured_state_summary = None
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            nonlocal captured_state_summary
            captured_state_summary = state_summary
            now = datetime.now(UTC)
            return {
                "version_id": "snap-scope",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "symbol_whitelist": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
                "risk_multiplier": 0.5,
                "per_symbol_max_position": 0.12,
                "max_leverage": 3,
                "market_mode": RunMode.NORMAL,
                "approval_state": ApprovalState.APPROVED,
                "source_reason": "cycle",
                "ttl_sec": 300,
            }

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_run_mode(RunMode.DEGRADED)
    runtime.last_snapshot = runtime.last_snapshot.model_copy(update={"symbol_whitelist": ["BTC-USDT-SWAP"]})
    runtime.runtime_store.set_symbol_runtime_summary(
        "BTC-USDT-SWAP",
        {"symbol": "BTC-USDT-SWAP", "mid_price": 100.1, "net_quantity": 1.0},
    )
    runtime.runtime_store.set_symbol_runtime_summary(
        "ETH-USDT-SWAP",
        {"symbol": "ETH-USDT-SWAP", "mid_price": 200.2, "net_quantity": 0.0},
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert captured_state_summary is not None
    assert captured_state_summary["symbol_summaries"] == [
        {"symbol": "BTC-USDT-SWAP", "mid_price": 100.1, "net_quantity": 1.0},
        {"symbol": "ETH-USDT-SWAP", "mid_price": 200.2, "net_quantity": 0.0},
    ]


def test_governor_runtime_publishes_snapshot_to_shared_redis_store(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)

    class _Runner:
        async def run(self, state_summary):
            return {
                "version_id": "snap-shared",
                "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "effective_from": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(
        governor_app,
        "build_snapshot_store",
        lambda settings: RedisSnapshotStore(redis_client=_FakeRedis()),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_run_mode(RunMode.DEGRADED)
    asyncio.run(governor_app._run_governor_cycle(runtime))

    stored = runtime.snapshot_store.get_latest_snapshot()
    assert stored is not None
    assert stored.version_id.startswith("governor-")


def test_governor_cycle_can_publish_snapshot_from_approved_research(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    monkeypatch.setattr(governor_app, "_MIN_RESEARCH_NET_PNL", 0.0)
    captured_state_summary = None

    definition = _sample_strategy_definition(parameter_set={"lookback_fast": 20, "lookback_slow": 60})
    research_package = StrategyPackage(
        strategy_package_id="pkg-001",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="long_only",
        entry_rules=definition["entry_rules"],
        exit_rules=definition["exit_rules"],
        position_sizing_rules=definition["position_sizing_rules"],
        risk_constraints=definition["risk_constraints"],
        parameter_set=definition["parameter_set"],
        backtest_summary={"total_return": 0.18},
        performance_summary={"sharpe": 1.4},
        failure_modes=["range_whipsaw"],
        invalidating_conditions=["liquidity_collapse"],
        research_reason="manual research run",
        strategy_definition=definition,
        score=67.5,
        score_basis="backtest_return_percent",
    )
    historical_rows = [
        {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
        {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 101.0},
        {"timestamp": datetime(2026, 4, 19, 0, 10, tzinfo=UTC), "close": 102.0},
        {"timestamp": datetime(2026, 4, 19, 0, 15, tzinfo=UTC), "close": 103.0},
        {"timestamp": datetime(2026, 4, 19, 0, 20, tzinfo=UTC), "close": 104.0},
    ]

    class _Runner:
        async def run(self, state_summary):
            nonlocal captured_state_summary
            captured_state_summary = state_summary
            now = datetime.now(UTC)
            return {
                "version_id": "snap-research",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    async def _approved_research_result(*_args, **_kwargs):
        return governor_app.ResearchCandidateBuildResult(
            status="candidate_built",
            historical_rows=historical_rows,
            candidates=[research_package],
        )

    monkeypatch.setattr(governor_app, "_prepare_research_candidates", _approved_research_result)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))

    runtime = governor_app.build_governor_runtime()
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert captured_state_summary is not None
    assert captured_state_summary["committee_summary"]["research_candidate_count"] == 1
    assert captured_state_summary["committee_summary"]["approved_research_candidates"] == ["pkg-001"]
    assert captured_state_summary["research_candidates"] == [research_package.model_dump(mode="json")]
    assert runtime.last_snapshot.source_reason == "approved research package"
    assert [snapshot.source_reason for snapshot in runtime.published_snapshots] == [
        "approved research package"
    ]
    assert len(runtime.history_store.written_rows["approval_records"]) == 1
    assert runtime.history_store.written_rows["approval_records"][0]["decision"] == "approved"


def test_governor_cycle_does_not_send_unapproved_research_downstream(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    monkeypatch.setattr(governor_app, "_MIN_RESEARCH_NET_PNL", 0.0)
    captured_state_summary = None
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    definition = _sample_strategy_definition(parameter_set={"lookback_fast": 20, "lookback_slow": 60})
    research_package = StrategyPackage(
        strategy_package_id="pkg-blocked",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="long_only",
        entry_rules=definition["entry_rules"],
        exit_rules=definition["exit_rules"],
        position_sizing_rules=definition["position_sizing_rules"],
        risk_constraints=definition["risk_constraints"],
        parameter_set=definition["parameter_set"],
        backtest_summary={"total_return": 0.18},
        performance_summary={"sharpe": 1.4},
        failure_modes=["range_whipsaw"],
        invalidating_conditions=["liquidity_collapse"],
        research_reason="manual research run",
        strategy_definition=definition,
        score=67.5,
        score_basis="backtest_return_percent",
    )
    historical_rows = [
        {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
        {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 101.0},
        {"timestamp": datetime(2026, 4, 19, 0, 10, tzinfo=UTC), "close": 102.0},
        {"timestamp": datetime(2026, 4, 19, 0, 15, tzinfo=UTC), "close": 103.0},
        {"timestamp": datetime(2026, 4, 19, 0, 20, tzinfo=UTC), "close": 104.0},
    ]

    class _Runner:
        async def run(self, state_summary):
            nonlocal captured_state_summary
            captured_state_summary = state_summary
            now = datetime.now(UTC)
            return {
                "version_id": "snap-blocked-research",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    original_build_committee_summary = governor_app.GovernorService.build_committee_summary

    def _blocked_committee_summary(self, expert_opinions, *, research_candidates=None):
        summary = original_build_committee_summary(
            self,
            expert_opinions,
            research_candidates=research_candidates,
        )
        if research_candidates:
            summary["approved_research_candidates"] = []
        return summary

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    async def _blocked_research_result(*_args, **_kwargs):
        return governor_app.ResearchCandidateBuildResult(
            status="candidate_built",
            historical_rows=historical_rows,
            candidates=[research_package],
        )

    monkeypatch.setattr(governor_app, "_prepare_research_candidates", _blocked_research_result)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(governor_app.GovernorService, "build_committee_summary", _blocked_committee_summary)

    runtime = governor_app.build_governor_runtime()
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert captured_state_summary is None
    assert runtime.last_snapshot.version_id == "bootstrap"
    assert runtime.published_snapshots == []
    assert runtime.runtime_store.get_pending_approval_summary() == {
        "pending_count": 0,
        "latest_strategy_package_id": history_store.written_rows["strategy_packages"][0]["strategy_package_id"],
        "latest_backtest_report_id": history_store.written_rows["backtest_reports"][0]["backtest_report_id"],
        "approval_status": "rejected",
    }
    assert len(history_store.written_rows["approval_records"]) == 1
    assert history_store.written_rows["approval_records"][0]["decision"] == "rejected"
    assert history_store.written_rows["governor_runs"][-1]["status"] == "approval_rejected"


def test_governor_cycle_auto_approves_research_after_validation_and_publishes(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    monkeypatch.setattr(governor_app, "_MIN_RESEARCH_NET_PNL", 0.0)
    _stub_market_data_client(monkeypatch, _build_market_candles([100.0, 101.0, 102.0, 103.0, 104.0]))
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-auto-approved",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    class _Provider:
        provider_name = ResearchProviderName.API

        async def generate_analysis(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return research_providers_module.ResearchProviderSuggestion(
                thesis="trend remains constructive",
                strategy_family="breakout",
                entry_signal="breakout_confirmed",
                exit_stop_loss_bps=50,
                exit_take_profit_bps=120,
                risk_fraction=0.0025,
                max_hold_minutes=60,
                failure_modes=[],
                invalidating_conditions=[],
            )

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(
        governor_app,
        "build_research_engine",
        lambda settings: StrategyResearchEngine(provider=_Provider()),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_symbol_runtime_summary(
        "BTC-USDT-SWAP",
        {"symbol": "BTC-USDT-SWAP", "mid_price": 100.0, "net_quantity": 0.0},
    )
    runtime.history_store.append_order_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:00:00Z", "price": 100.0}
    )
    runtime.history_store.append_fill_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:05:00Z", "price": 101.0}
    )
    runtime.history_store.append_position_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:10:00Z", "mark_price": 103.0}
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert len(runtime.published_snapshots) == 1
    assert runtime.last_snapshot.version_id.startswith("governor-")
    assert len(history_store.written_rows["strategy_packages"]) >= 3
    assert len(history_store.written_rows["backtest_reports"]) >= 3
    assert len(history_store.written_rows["approval_records"]) == 1
    assert history_store.written_rows["approval_records"][0]["decision"] == "approved"
    assert len(history_store.written_rows["strategy_snapshots"]) == 1
    approved_package_id = history_store.written_rows["approval_records"][0]["strategy_package_id"]
    approved_backtest_report_id = history_store.written_rows["approval_records"][0]["backtest_report_id"]
    assert runtime.runtime_store.get_pending_approval_summary() == {
        "pending_count": 0,
        "latest_strategy_package_id": approved_package_id,
        "latest_backtest_report_id": approved_backtest_report_id,
        "approval_status": "approved",
    }
    expected_candidate_count = len(history_store.written_rows["strategy_packages"])
    assert history_store.written_rows["governor_runs"] == [
        {
            "version_id": runtime.last_snapshot.version_id,
            "status": "published",
            "error": None,
            "research_provider": "api",
            "research_status": "candidate_built",
            "research_provider_success": True,
            "research_error": None,
            "research_candidate_count": expected_candidate_count,
            "approved_research_candidate_ids": [approved_package_id],
            "validation_status": "succeeded",
            "validation_error": None,
            "approval_status": "approved",
            "approval_error": None,
            "backtest_report_id": approved_backtest_report_id,
            "approval_record_id": history_store.written_rows["approval_records"][0]["approval_record_id"],
        }
    ]


def test_governor_cycle_selects_highest_net_pnl_candidate_above_threshold(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-auto-approved",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    low_definition = _sample_strategy_definition()
    low_candidate = StrategyPackage(
        strategy_package_id="pkg-low",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.SCHEDULE,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="long_only",
        entry_rules=low_definition["entry_rules"],
        exit_rules=low_definition["exit_rules"],
        position_sizing_rules=low_definition["position_sizing_rules"],
        risk_constraints=low_definition["risk_constraints"],
        parameter_set=low_definition["parameter_set"],
        backtest_summary={"row_count": 10},
        performance_summary={"return_percent": 0.2},
        failure_modes=[],
        invalidating_conditions=[],
        research_reason="low candidate",
        strategy_definition=low_definition,
        score=67.5,
        score_basis="backtest_return_percent",
    )
    high_candidate = low_candidate.model_copy(
        update={
            "strategy_package_id": "pkg-high",
            "exit_rules": {
                "any": [
                    {"op": "crosses_below", "left": "close", "right": "sma_20"},
                    {"op": "take_profit_bps", "value": 180},
                    {"op": "stop_loss_bps", "value": 35},
                ]
            },
            "position_sizing_rules": {"risk_fraction": 0.004},
            "parameter_set": {
                **low_candidate.parameter_set,
                "take_profit_bps": 180,
                "stop_loss_bps": 35,
                "risk_fraction": 0.004,
            },
            "strategy_definition": low_candidate.strategy_definition.model_copy(
                update={
                    "exit_rules": {
                        "any": [
                            {"op": "crosses_below", "left": "close", "right": "sma_20"},
                            {"op": "take_profit_bps", "value": 180},
                            {"op": "stop_loss_bps", "value": 35},
                        ]
                    },
                    "position_sizing_rules": {"risk_fraction": 0.004},
                    "parameter_set": {
                        **low_candidate.strategy_definition.parameter_set,
                        "take_profit_bps": 180,
                        "stop_loss_bps": 35,
                        "risk_fraction": 0.004,
                    },
                }
            ),
            "research_reason": "high candidate",
        }
    )
    historical_rows = [
        {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
        {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 101.0},
        {"timestamp": datetime(2026, 4, 19, 0, 10, tzinfo=UTC), "close": 102.0},
    ]

    async def _candidate_result(*_args, **_kwargs):
        return governor_app.ResearchCandidateBuildResult(
            status="candidate_built",
            historical_rows=historical_rows,
            candidates=[low_candidate, high_candidate],
        )

    class _BacktestValidator:
        def validate(self, *, package, historical_rows):
            net_pnl = 0.55 if package.strategy_package_id == "pkg-low" else 0.82
            return BacktestReport(
                backtest_report_id=f"{package.strategy_package_id}-report",
                strategy_package_id=package.strategy_package_id,
                strategy_def_id=package.strategy_definition.strategy_def_id,
                symbol_scope=package.symbol_scope,
                dataset_range=BacktestDatasetRange(
                    start=historical_rows[0]["timestamp"],
                    end=historical_rows[-1]["timestamp"],
                    regime_fit=RegimeFit.ALIGNED,
                ),
                sample_count=len(historical_rows),
                trade_count=120,
                trade_count_sufficiency=TradeCountSufficiency.SUFFICIENT,
                net_pnl=net_pnl,
                return_percent=net_pnl * 100,
                max_drawdown=0.1,
                win_rate=0.5,
                profit_factor=1.3,
                stability_score=0.8,
                overfit_risk=OverfitRisk.LOW,
                failure_modes=[],
                invalidating_conditions=[],
                generated_at=historical_rows[-1]["timestamp"],
            )

    monkeypatch.setattr(governor_app, "_prepare_research_candidates", _candidate_result)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.backtest_validator = _BacktestValidator()

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert len(history_store.written_rows["strategy_packages"]) == 2
    assert len(history_store.written_rows["backtest_reports"]) == 2
    assert history_store.written_rows["approval_records"][0]["strategy_package_id"] == "pkg-high"
    assert history_store.written_rows["governor_runs"][-1]["approved_research_candidate_ids"] == ["pkg-high"]
    assert runtime.runtime_store.get_latest_approved_package_summary()["latest_strategy_package_id"] == "pkg-high"


def test_governor_cycle_rejects_all_candidates_below_required_net_pnl(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    candidate_definition = _sample_strategy_definition(
        strategy_family="breakout",
        directionality="short_only",
        parameter_set={"lookback": 20, "signal_mode": "breakout_confirmed"},
    )
    candidate = StrategyPackage(
        strategy_package_id="pkg-low",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.SCHEDULE,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="short_only",
        entry_rules=candidate_definition["entry_rules"],
        exit_rules=candidate_definition["exit_rules"],
        position_sizing_rules=candidate_definition["position_sizing_rules"],
        risk_constraints=candidate_definition["risk_constraints"],
        parameter_set=candidate_definition["parameter_set"],
        backtest_summary={"row_count": 10},
        performance_summary={"return_percent": 0.2},
        failure_modes=[],
        invalidating_conditions=[],
        research_reason="low candidate",
        strategy_definition=candidate_definition,
        score=67.5,
        score_basis="backtest_return_percent",
    )
    historical_rows = [
        {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
        {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 101.0},
        {"timestamp": datetime(2026, 4, 19, 0, 10, tzinfo=UTC), "close": 102.0},
    ]

    async def _candidate_result(*_args, **_kwargs):
        return governor_app.ResearchCandidateBuildResult(
            status="candidate_built",
            historical_rows=historical_rows,
            candidates=[candidate],
        )

    class _BacktestValidator:
        def validate(self, *, package, historical_rows):
            return BacktestReport(
                backtest_report_id=f"{package.strategy_package_id}-report",
                strategy_package_id=package.strategy_package_id,
                strategy_def_id=package.strategy_definition.strategy_def_id,
                symbol_scope=package.symbol_scope,
                dataset_range=BacktestDatasetRange(
                    start=historical_rows[0]["timestamp"],
                    end=historical_rows[-1]["timestamp"],
                    regime_fit=RegimeFit.ALIGNED,
                ),
                sample_count=len(historical_rows),
                trade_count=120,
                trade_count_sufficiency=TradeCountSufficiency.SUFFICIENT,
                net_pnl=0.31,
                return_percent=31.0,
                max_drawdown=0.1,
                win_rate=0.5,
                profit_factor=1.1,
                stability_score=0.7,
                overfit_risk=OverfitRisk.LOW,
                failure_modes=[],
                invalidating_conditions=[],
                generated_at=historical_rows[-1]["timestamp"],
            )

    class _Runner:
        async def run(self, state_summary):
            raise AssertionError("governor client should not run when all research candidates fail net_pnl filter")

    monkeypatch.setattr(governor_app, "_prepare_research_candidates", _candidate_result)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.backtest_validator = _BacktestValidator()

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.last_snapshot.version_id == "bootstrap"
    assert history_store.written_rows["governor_runs"][-1]["status"] == "validation_failed"
    assert "minimum quality threshold 0.5" in history_store.written_rows["governor_runs"][-1]["validation_error"]
    assert runtime.runtime_store.get_pending_approval_summary() == {
        "pending_count": 0,
        "latest_strategy_package_id": "pkg-low",
        "approval_status": "validation_failed",
    }
    assert runtime.runtime_store.get_backtest_health_summary()["status"] == "candidate_rejected_low_quality"


def test_governor_cycle_skips_invalid_candidates_and_keeps_evaluating_remaining_candidates(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    invalid_definition = _sample_strategy_definition(
        strategy_family="mean_reversion",
        parameter_set={"lookback": 1, "signal_mode": "range_retest"},
    )
    invalid_candidate = StrategyPackage(
        strategy_package_id="pkg-invalid",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.SCHEDULE,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment_scope=["range"],
        strategy_family="mean_reversion",
        directionality="long_only",
        entry_rules=invalid_definition["entry_rules"],
        exit_rules=invalid_definition["exit_rules"],
        position_sizing_rules=invalid_definition["position_sizing_rules"],
        risk_constraints=invalid_definition["risk_constraints"],
        parameter_set=invalid_definition["parameter_set"],
        backtest_summary={"row_count": 10},
        performance_summary={"return_percent": 0.2},
        failure_modes=[],
        invalidating_conditions=[],
        research_reason="invalid candidate",
        strategy_definition=invalid_definition,
        score=67.5,
        score_basis="backtest_return_percent",
    )
    valid_candidate = invalid_candidate.model_copy(
        update={
            "strategy_package_id": "pkg-valid",
            "market_environment_scope": ["trend"],
            "strategy_family": "breakout",
            "entry_rules": invalid_candidate.strategy_definition.model_copy(
                update={"strategy_family": "breakout"}
            ).entry_rules,
            "exit_rules": invalid_candidate.strategy_definition.model_copy(
                update={"strategy_family": "breakout"}
            ).exit_rules,
            "position_sizing_rules": invalid_candidate.position_sizing_rules,
            "risk_constraints": invalid_candidate.risk_constraints,
            "parameter_set": {
                **invalid_candidate.parameter_set,
                "lookback": 2,
                "signal_mode": "breakout_confirmed",
            },
            "strategy_definition": invalid_candidate.strategy_definition.model_copy(
                update={
                    "strategy_family": "breakout",
                    "parameter_set": {
                        **invalid_candidate.strategy_definition.parameter_set,
                        "lookback": 2,
                        "signal_mode": "breakout_confirmed",
                    },
                    "entry_rules": invalid_candidate.strategy_definition.entry_rules,
                    "exit_rules": invalid_candidate.strategy_definition.exit_rules,
                }
            ),
            "research_reason": "valid candidate",
        }
    )
    historical_rows = [
        {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
        {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 101.0},
        {"timestamp": datetime(2026, 4, 19, 0, 10, tzinfo=UTC), "close": 102.0},
    ]

    async def _candidate_result(*_args, **_kwargs):
        return governor_app.ResearchCandidateBuildResult(
            status="candidate_built",
            historical_rows=historical_rows,
            candidates=[invalid_candidate, valid_candidate],
            candidate_historical_rows={
                "pkg-invalid": historical_rows,
                "pkg-valid": historical_rows,
            },
        )

    class _BacktestValidator:
        def validate(self, *, package, historical_rows):
            if package.strategy_package_id == "pkg-invalid":
                raise ValueError("range_retest requires lookback >= 2")
            return BacktestReport(
                backtest_report_id=f"{package.strategy_package_id}-report",
                strategy_package_id=package.strategy_package_id,
                strategy_def_id=package.strategy_definition.strategy_def_id,
                symbol_scope=package.symbol_scope,
                dataset_range=BacktestDatasetRange(
                    start=historical_rows[0]["timestamp"],
                    end=historical_rows[-1]["timestamp"],
                    regime_fit=RegimeFit.ALIGNED,
                ),
                sample_count=len(historical_rows),
                trade_count=120,
                trade_count_sufficiency=TradeCountSufficiency.SUFFICIENT,
                net_pnl=0.6,
                return_percent=60.0,
                max_drawdown=0.1,
                win_rate=0.5,
                profit_factor=1.4,
                stability_score=0.8,
                overfit_risk=OverfitRisk.LOW,
                failure_modes=[],
                invalidating_conditions=[],
                generated_at=historical_rows[-1]["timestamp"],
            )

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-valid",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    monkeypatch.setattr(governor_app, "_prepare_research_candidates", _candidate_result)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.backtest_validator = _BacktestValidator()

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert history_store.written_rows["governor_runs"][-1]["status"] == "published"
    assert history_store.written_rows["approval_records"][0]["strategy_package_id"] == "pkg-valid"


def test_governor_cycle_skips_schedule_search_while_observing_approved_strategy(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            raise AssertionError("governor client should stay idle during observing schedule cycles")

    async def _unexpected_candidates(*_args, **_kwargs):
        raise AssertionError("research search should not run during observing schedule cycles")

    monkeypatch.setattr(governor_app, "_prepare_research_candidates", _unexpected_candidates)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_strategy_search_mode("observe_until_invalidated")
    runtime.runtime_store.set_latest_approved_package_summary(
        {"latest_strategy_package_id": "pkg-approved", "approved_at": "2026-04-20T00:00:00Z"}
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert history_store.written_rows["governor_runs"][-1]["status"] == "observing"
    assert runtime.runtime_store.get_strategy_search_mode() == "observe_until_invalidated"


def test_governor_cycle_restarts_search_when_observing_strategy_is_invalidated(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    restart_definition = _sample_strategy_definition()
    candidate = StrategyPackage(
        strategy_package_id="pkg-low",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.SCHEDULE,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="long_only",
        entry_rules=restart_definition["entry_rules"],
        exit_rules=restart_definition["exit_rules"],
        position_sizing_rules=restart_definition["position_sizing_rules"],
        risk_constraints=restart_definition["risk_constraints"],
        parameter_set=restart_definition["parameter_set"],
        backtest_summary={"row_count": 10},
        performance_summary={"return_percent": 0.2},
        failure_modes=[],
        invalidating_conditions=[],
        research_reason="low candidate",
        strategy_definition=restart_definition,
        score=67.5,
        score_basis="backtest_return_percent",
    )
    historical_rows = [
        {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
        {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 101.0},
        {"timestamp": datetime(2026, 4, 19, 0, 10, tzinfo=UTC), "close": 102.0},
    ]

    async def _candidate_result(*_args, **_kwargs):
        return governor_app.ResearchCandidateBuildResult(
            status="candidate_built",
            historical_rows=historical_rows,
            candidates=[candidate],
        )

    class _BacktestValidator:
        def validate(self, *, package, historical_rows):
            return BacktestReport(
                backtest_report_id=f"{package.strategy_package_id}-report",
                strategy_package_id=package.strategy_package_id,
                strategy_def_id=package.strategy_definition.strategy_def_id,
                symbol_scope=package.symbol_scope,
                dataset_range=BacktestDatasetRange(
                    start=historical_rows[0]["timestamp"],
                    end=historical_rows[-1]["timestamp"],
                    regime_fit=RegimeFit.ALIGNED,
                ),
                sample_count=len(historical_rows),
                trade_count=120,
                trade_count_sufficiency=TradeCountSufficiency.SUFFICIENT,
                net_pnl=0.31,
                return_percent=31.0,
                max_drawdown=0.1,
                win_rate=0.5,
                profit_factor=1.1,
                stability_score=0.7,
                overfit_risk=OverfitRisk.LOW,
                failure_modes=[],
                invalidating_conditions=[],
                generated_at=historical_rows[-1]["timestamp"],
            )

    class _Runner:
        async def run(self, state_summary):
            raise AssertionError("governor client should not run when restarted search still fails threshold")

    monkeypatch.setattr(governor_app, "_prepare_research_candidates", _candidate_result)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.backtest_validator = _BacktestValidator()
    runtime.runtime_store.set_strategy_search_mode("observe_until_invalidated")
    runtime.runtime_store.set_latest_approved_package_summary(
        {"latest_strategy_package_id": "pkg-approved", "approved_at": "2026-04-20T00:00:00Z"}
    )
    runtime.runtime_store.set_run_mode(RunMode.DEGRADED)

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert history_store.written_rows["governor_runs"][-1]["status"] == "validation_failed"
    assert runtime.runtime_store.get_strategy_search_mode() == "search_until_qualified"


def test_governor_cycle_auto_approves_research_with_multi_symbol_runtime(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_OKX_SYMBOLS", "BTC-USDT-SWAP,ETH-USDT-SWAP")
    monkeypatch.setattr(governor_app, "_MIN_RESEARCH_NET_PNL", 0.0)
    _stub_market_data_client(monkeypatch, _build_market_candles([100.0, 101.0, 102.0, 103.0, 104.0]))
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-auto-approved-multi-symbol",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "symbol_whitelist": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
                "risk_multiplier": 0.5,
                "per_symbol_max_position": 0.12,
                "max_leverage": 3,
                "market_mode": RunMode.NORMAL,
                "approval_state": ApprovalState.APPROVED,
                "source_reason": "cycle",
                "ttl_sec": 300,
            }

    class _Provider:
        provider_name = ResearchProviderName.API

        async def generate_analysis(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            assert len(symbol_scope) == 1
            assert symbol_scope[0] in {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
            return research_providers_module.ResearchProviderSuggestion(
                thesis="trend remains constructive",
                strategy_family="breakout",
                entry_signal="breakout_confirmed",
                exit_stop_loss_bps=50,
                exit_take_profit_bps=120,
                risk_fraction=0.0025,
                max_hold_minutes=60,
                failure_modes=[],
                invalidating_conditions=[],
            )

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(
        governor_app,
        "build_research_engine",
        lambda settings: StrategyResearchEngine(provider=_Provider()),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_symbol_runtime_summary(
        "BTC-USDT-SWAP",
        {"symbol": "BTC-USDT-SWAP", "mid_price": 100.0, "net_quantity": 0.0},
    )
    runtime.runtime_store.set_symbol_runtime_summary(
        "ETH-USDT-SWAP",
        {"symbol": "ETH-USDT-SWAP", "mid_price": 200.0, "net_quantity": 0.0},
    )
    runtime.history_store.append_order_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:00:00Z", "price": 100.0}
    )
    runtime.history_store.append_fill_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:05:00Z", "price": 101.0}
    )
    runtime.history_store.append_position_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:10:00Z", "mark_price": 103.0}
    )
    runtime.history_store.append_order_fact(
        {"symbol": "ETH-USDT-SWAP", "generated_at": "2026-04-19T00:00:00Z", "price": 200.0}
    )
    runtime.history_store.append_fill_fact(
        {"symbol": "ETH-USDT-SWAP", "generated_at": "2026-04-19T00:05:00Z", "price": 198.0}
    )
    runtime.history_store.append_position_fact(
        {"symbol": "ETH-USDT-SWAP", "generated_at": "2026-04-19T00:10:00Z", "mark_price": 202.0}
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert len(runtime.published_snapshots) == 1
    assert history_store.written_rows["governor_runs"][-1]["status"] == "published"
    assert history_store.written_rows["governor_runs"][-1]["validation_status"] == "succeeded"
    assert history_store.written_rows["governor_runs"][-1]["approval_status"] == "approved"
    assert {tuple(row["symbol_scope"]) for row in history_store.written_rows["strategy_packages"]} == {
        ("BTC-USDT-SWAP",),
        ("ETH-USDT-SWAP",),
    }


def test_governor_cycle_publishes_auto_approved_research_with_guardrails(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    monkeypatch.setattr(governor_app, "_MIN_RESEARCH_NET_PNL", 0.0)
    _stub_market_data_client(monkeypatch, _build_market_candles([100.0, 101.0, 102.0, 103.0, 104.0]))
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-approved-research",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "symbol_whitelist": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
                "risk_multiplier": 0.45,
                "per_symbol_max_position": 0.12,
                "max_leverage": 3,
                "market_mode": RunMode.NORMAL,
                "approval_state": ApprovalState.APPROVED,
                "source_reason": "cycle",
                "ttl_sec": 300,
            }

    class _Provider:
        provider_name = ResearchProviderName.API

        async def generate_analysis(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return research_providers_module.ResearchProviderSuggestion(
                thesis="trend remains constructive",
                strategy_family="breakout",
                entry_signal="breakout_confirmed",
                exit_stop_loss_bps=50,
                exit_take_profit_bps=120,
                risk_fraction=0.0025,
                max_hold_minutes=60,
                failure_modes=[],
                invalidating_conditions=[],
            )

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(
        governor_app,
        "build_research_engine",
        lambda settings: StrategyResearchEngine(provider=_Provider()),
    )

    original_build_committee_summary = governor_app.GovernorService.build_committee_summary

    def _guardrailed_committee_summary(self, expert_opinions, *, research_candidates=None):
        summary = original_build_committee_summary(
            self,
            expert_opinions,
            research_candidates=research_candidates,
        )
        if research_candidates:
            summary["approved_research_candidates"] = [research_candidates[0].strategy_package_id]
            summary["recommended_mode_floor"] = "degraded"
        return summary

    monkeypatch.setattr(governor_app.GovernorService, "build_committee_summary", _guardrailed_committee_summary)

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_symbol_runtime_summary(
        "BTC-USDT-SWAP",
        {"symbol": "BTC-USDT-SWAP", "mid_price": 100.0, "net_quantity": 0.0},
    )
    runtime.history_store.append_order_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:00:00Z", "price": 100.0}
    )
    runtime.history_store.append_fill_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:05:00Z", "price": 101.0}
    )
    runtime.history_store.append_position_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:10:00Z", "mark_price": 103.0}
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))

    package_id = history_store.written_rows["approval_records"][0]["strategy_package_id"]
    backtest_report_id = history_store.written_rows["approval_records"][0]["backtest_report_id"]
    approval_record_id = history_store.written_rows["approval_records"][0]["approval_record_id"]
    assert runtime.last_snapshot.source_reason == "approved research package"
    assert runtime.last_snapshot.market_mode == RunMode.DEGRADED
    assert len(runtime.history_store.written_rows["approval_records"]) == 1
    assert history_store.written_rows["approval_records"][0]["decision"] == "approved_with_guardrails"
    assert len(history_store.written_rows["strategy_packages"]) >= 3
    assert len(history_store.written_rows["backtest_reports"]) >= 3
    assert len(runtime.published_snapshots) == 1
    assert history_store.written_rows["strategy_snapshots"] == [
        {
            "version_id": runtime.last_snapshot.version_id,
            "market_mode": "degraded",
            "approval_state": "approved",
            "symbol_whitelist": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
            "risk_multiplier": 0.45,
            "per_symbol_max_position": 0.12,
            "max_leverage": 3,
            "source_reason": "approved research package",
            "strategy_package_id": package_id,
            "backtest_report_id": backtest_report_id,
            "approval_record_id": approval_record_id,
            "approval_decision": "approved_with_guardrails",
            "guardrails": {"market_mode": "degraded"},
        }
    ]
    assert runtime.runtime_store.get_latest_approved_package_summary() == {
        "latest_strategy_package_id": package_id,
        "backtest_report_id": backtest_report_id,
        "approval_record_id": approval_record_id,
        "approved_at": history_store.written_rows["approval_records"][0]["created_at"],
        "approval_decision": "approved_with_guardrails",
    }
    expected_candidate_count = len(history_store.written_rows["strategy_packages"])
    assert history_store.written_rows["governor_runs"][-1] == {
        "version_id": runtime.last_snapshot.version_id,
        "status": "published",
        "error": None,
        "research_provider": "api",
        "research_status": "candidate_built",
        "research_provider_success": True,
        "research_error": None,
        "research_candidate_count": expected_candidate_count,
        "approved_research_candidate_ids": [package_id],
        "validation_status": "succeeded",
        "validation_error": None,
        "approval_status": "approved_with_guardrails",
        "approval_error": None,
        "backtest_report_id": backtest_report_id,
        "approval_record_id": approval_record_id,
    }


def test_governor_cycle_validation_failure_blocks_publication_explicitly(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    _stub_market_data_client(monkeypatch, _build_market_candles([100.0, 101.0, 102.0, 103.0, 104.0]))
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-validation-failed",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )
    class _Provider:
        provider_name = ResearchProviderName.API

        async def generate_analysis(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return research_providers_module.ResearchProviderSuggestion(
                thesis="trend remains constructive",
                strategy_family="breakout",
                entry_signal="breakout_confirmed",
                exit_stop_loss_bps=50,
                exit_take_profit_bps=120,
                risk_fraction=0.0025,
                max_hold_minutes=60,
                failure_modes=[],
                invalidating_conditions=[],
            )

    monkeypatch.setattr(
        governor_app,
        "build_research_engine",
        lambda settings: StrategyResearchEngine(provider=_Provider()),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_symbol_runtime_summary(
        "BTC-USDT-SWAP",
        {"symbol": "BTC-USDT-SWAP", "mid_price": 100.0, "net_quantity": 0.0},
    )
    runtime.history_store.append_order_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:00:00Z", "price": 100.0}
    )
    runtime.history_store.append_fill_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:05:00Z", "price": 101.0}
    )
    runtime.history_store.append_position_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:10:00Z", "mark_price": 103.0}
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))

    package_id = history_store.written_rows["strategy_packages"][0]["strategy_package_id"]
    backtest_report_id = history_store.written_rows["backtest_reports"][0]["backtest_report_id"]
    initial_published_snapshots = list(runtime.published_snapshots)
    initial_strategy_snapshots = list(history_store.written_rows["strategy_snapshots"])
    monkeypatch.setattr(
        runtime.backtest_validator,
        "validate",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("backtest exploded")),
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.published_snapshots == initial_published_snapshots
    assert history_store.written_rows["governor_runs"][-1]["status"] == "validation_failed"
    assert history_store.written_rows["governor_runs"][-1]["approval_status"] == "validation_failed"
    assert history_store.written_rows["governor_runs"][-1]["approval_error"] is None
    assert history_store.written_rows["governor_runs"][-1]["validation_error"] == "backtest exploded"
    assert history_store.written_rows["strategy_snapshots"] == initial_strategy_snapshots


def test_governor_cycle_auto_rejects_non_publishable_research(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    _stub_market_data_client(monkeypatch, _build_market_candles([100.0, 101.0, 102.0, 103.0, 104.0]))
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-non-publishable",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    class _Provider:
        provider_name = ResearchProviderName.API

        async def generate_analysis(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return research_providers_module.ResearchProviderSuggestion(
                thesis="trend remains constructive",
                strategy_family="breakout",
                entry_signal="breakout_confirmed",
                exit_stop_loss_bps=50,
                exit_take_profit_bps=120,
                risk_fraction=0.0025,
                max_hold_minutes=60,
                failure_modes=[],
                invalidating_conditions=[],
            )

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(
        governor_app,
        "build_research_engine",
        lambda settings: StrategyResearchEngine(provider=_Provider()),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_symbol_runtime_summary(
        "BTC-USDT-SWAP",
        {"symbol": "BTC-USDT-SWAP", "mid_price": 100.0, "net_quantity": 0.0},
    )
    runtime.history_store.append_order_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:00:00Z", "price": 100.0}
    )
    runtime.history_store.append_fill_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:05:00Z", "price": 101.0}
    )
    runtime.history_store.append_position_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:10:00Z", "mark_price": 103.0}
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))

    package_id = history_store.written_rows["strategy_packages"][0]["strategy_package_id"]
    backtest_report_id = history_store.written_rows["backtest_reports"][0]["backtest_report_id"]
    initial_published_snapshots = list(runtime.published_snapshots)
    original_build_committee_summary = governor_app.GovernorService.build_committee_summary

    def _blocked_committee_summary(self, expert_opinions, *, research_candidates=None):
        summary = original_build_committee_summary(
            self,
            expert_opinions,
            research_candidates=research_candidates,
        )
        if research_candidates:
            summary["approved_research_candidates"] = []
        return summary

    monkeypatch.setattr(governor_app.GovernorService, "build_committee_summary", _blocked_committee_summary)
    original_validate = runtime.backtest_validator.validate

    def _candidate_beats_baseline(*, package, historical_rows):
        report = original_validate(package=package, historical_rows=historical_rows)
        if package.strategy_package_id == package_id:
            return report
        return report.model_copy(update={"net_pnl": report.net_pnl + 1.0})

    monkeypatch.setattr(runtime.backtest_validator, "validate", _candidate_beats_baseline)
    baseline_package = governor_app.StrategyPackage.model_validate(
        history_store.written_rows["strategy_packages"][0]
    )

    async def _second_candidate(*_args, **_kwargs):
        return governor_app.ResearchCandidateBuildResult(
            status="candidate_built",
            historical_rows=await governor_app._fetch_okx_historical_rows(runtime, "BTC-USDT-SWAP"),
            candidates=[baseline_package.model_copy(update={"strategy_package_id": "pkg-blocked-next"})],
        )

    monkeypatch.setattr(governor_app, "_prepare_research_candidates", _second_candidate)

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.published_snapshots == initial_published_snapshots
    assert history_store.written_rows["governor_runs"][-1]["status"] == "approval_rejected"
    assert history_store.written_rows["governor_runs"][-1]["approval_status"] == "rejected"
    assert history_store.written_rows["approval_records"][-1]["decision"] == "rejected"


def test_governor_cycle_records_validation_failed_status(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    _stub_market_data_client(monkeypatch, _build_market_candles([100.0, 101.0, 102.0, 103.0, 104.0]))
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            raise AssertionError("governor client should not run when validation fails")

    class _Provider:
        provider_name = ResearchProviderName.API

        async def generate_analysis(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return research_providers_module.ResearchProviderSuggestion(
                thesis="trend remains constructive",
                strategy_family="breakout",
                entry_signal="breakout_confirmed",
                exit_stop_loss_bps=50,
                exit_take_profit_bps=120,
                risk_fraction=0.0025,
                max_hold_minutes=60,
                failure_modes=[],
                invalidating_conditions=[],
            )

    class _BrokenValidator:
        def validate(self, *, package, historical_rows):
            raise ValueError("historical_rows timestamps must be unique")

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(
        governor_app,
        "build_research_engine",
        lambda settings: StrategyResearchEngine(provider=_Provider()),
    )
    monkeypatch.setattr(governor_app, "build_backtest_validator", lambda: _BrokenValidator())

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_symbol_runtime_summary(
        "BTC-USDT-SWAP",
        {"symbol": "BTC-USDT-SWAP", "mid_price": 100.0, "net_quantity": 0.0},
    )
    runtime.history_store.append_order_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:00:00Z", "price": 100.0}
    )
    runtime.history_store.append_fill_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:05:00Z", "price": 101.0}
    )
    runtime.history_store.append_position_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:10:00Z", "mark_price": 103.0}
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.published_snapshots == []
    assert len(history_store.written_rows["strategy_packages"]) >= 3
    assert history_store.written_rows["backtest_reports"] == []
    assert history_store.written_rows["governor_runs"][-1]["status"] == "validation_failed"
    assert history_store.written_rows["governor_runs"][-1]["research_candidate_count"] == len(
        history_store.written_rows["strategy_packages"]
    )
    assert history_store.written_rows["governor_runs"][-1]["validation_status"] == "failed"
    assert history_store.written_rows["governor_runs"][-1]["approval_status"] == "validation_failed"


def test_governor_cycle_drops_candidate_that_underperforms_current_strategy(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    _stub_market_data_client(monkeypatch, _build_market_candles([100.0, 101.0, 102.0, 103.0, 104.0]))
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            raise AssertionError("governor client should not run when candidate underperforms baseline")

    class _Provider:
        provider_name = ResearchProviderName.API

        async def generate_analysis(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return research_providers_module.ResearchProviderSuggestion(
                thesis="candidate is weaker than baseline",
                strategy_family="breakout",
                entry_signal="breakout_confirmed",
                exit_stop_loss_bps=50,
                exit_take_profit_bps=120,
                risk_fraction=0.0025,
                max_hold_minutes=60,
                failure_modes=[],
                invalidating_conditions=[],
            )

    class _Validator:
        def validate(self, *, package, historical_rows):
            report_id = f"{package.strategy_package_id}-report"
            payload = {
                "backtest_report_id": report_id,
                "strategy_package_id": package.strategy_package_id,
                "strategy_def_id": package.strategy_definition.strategy_def_id,
                "symbol_scope": package.symbol_scope,
                "dataset_range": {
                    "start": historical_rows[0]["timestamp"],
                    "end": historical_rows[-1]["timestamp"],
                    "regime_fit": "aligned",
                },
                "sample_count": len(historical_rows),
                "trade_count": 1,
                "trade_count_sufficiency": "insufficient",
                "net_pnl": 2.0 if package.strategy_package_id == "pkg-current" else 1.0,
                "return_percent": 200.0 if package.strategy_package_id == "pkg-current" else 100.0,
                "max_drawdown": 0.0,
                "win_rate": 1.0,
                "profit_factor": 999.0,
                "stability_score": 0.875,
                "overfit_risk": "high",
                "failure_modes": [],
                "invalidating_conditions": [],
                "generated_at": historical_rows[-1]["timestamp"],
            }
            return governor_app.BacktestReport.model_validate(payload)

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(
        governor_app,
        "build_research_engine",
        lambda settings: StrategyResearchEngine(provider=_Provider()),
    )
    monkeypatch.setattr(governor_app, "build_backtest_validator", lambda: _Validator())

    runtime = governor_app.build_governor_runtime()
    runtime.last_snapshot = runtime.last_snapshot.model_copy(
        update={
            "version_id": "snap-current",
            "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": False},
            "source_reason": "approved research package",
        }
    )
    restart_definition = _sample_strategy_definition()
    history_store.append_strategy_package(
        {
            "strategy_package_id": "pkg-current",
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "trigger": "schedule",
            "symbol_scope": ["BTC-USDT-SWAP"],
            "market_environment_scope": ["trend"],
            "strategy_family": "breakout",
            "directionality": "long_only",
            "entry_rules": restart_definition["entry_rules"],
            "exit_rules": restart_definition["exit_rules"],
            "position_sizing_rules": restart_definition["position_sizing_rules"],
            "risk_constraints": restart_definition["risk_constraints"],
            "parameter_set": restart_definition["parameter_set"],
            "backtest_summary": {},
            "performance_summary": {},
            "failure_modes": [],
            "invalidating_conditions": [],
            "research_reason": "baseline",
            "strategy_definition": restart_definition,
            "score": 67.5,
            "score_basis": "backtest_return_percent",
        }
    )
    history_store.append_strategy_snapshot(
        {
            "version_id": "snap-current",
            "market_mode": "normal",
            "approval_state": "approved",
            "symbol_whitelist": ["BTC-USDT-SWAP"],
            "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": False},
            "risk_multiplier": 0.5,
            "per_symbol_max_position": 0.12,
            "max_leverage": 3,
            "source_reason": "approved research package",
            "strategy_package_id": "pkg-current",
        }
    )
    runtime.runtime_store.set_symbol_runtime_summary(
        "BTC-USDT-SWAP",
        {"symbol": "BTC-USDT-SWAP", "mid_price": 100.0, "net_quantity": 0.0},
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.published_snapshots == []
    assert history_store.written_rows["approval_records"] == []
    assert history_store.written_rows["governor_runs"][-1]["status"] == "validation_failed"
    assert history_store.written_rows["governor_runs"][-1]["approval_status"] == "validation_failed"
    assert "did not exceed current strategy" in history_store.written_rows["governor_runs"][-1]["validation_error"]


def test_governor_runtime_records_snapshot_publication_for_notifier(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-audit",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "symbol_whitelist": ["BTC-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
                "risk_multiplier": 0.5,
                "per_symbol_max_position": 0.12,
                "max_leverage": 3,
                "market_mode": RunMode.DEGRADED,
                "approval_state": ApprovalState.APPROVED,
                "source_reason": "cycle",
                "ttl_sec": 300,
            }

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_run_mode(RunMode.DEGRADED)
    asyncio.run(governor_app._run_governor_cycle(runtime))

    snapshot_version = history_store.written_rows["strategy_snapshots"][0]["version_id"]
    assert snapshot_version.startswith("governor-")
    assert history_store.written_rows["strategy_snapshots"] == [
        {
            "version_id": snapshot_version,
            "market_mode": "normal",
            "approval_state": "approved",
            "symbol_whitelist": ["BTC-USDT-SWAP"],
            "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
            "risk_multiplier": 0.5,
            "per_symbol_max_position": 0.12,
            "max_leverage": 3,
            "source_reason": "cycle|guardrailed",
        }
    ]
    assert history_store.written_rows["governor_runs"] == [
        {
            "version_id": snapshot_version,
            "status": "published",
            "error": None,
            "research_provider": "api",
            "research_status": "missing_symbol_summaries",
            "research_provider_success": None,
            "research_error": None,
            "research_candidate_count": 0,
            "approved_research_candidate_ids": [],
            "validation_status": "not_requested",
            "validation_error": None,
            "approval_status": "not_requested",
            "approval_error": None,
            "backtest_report_id": None,
            "approval_record_id": None,
        }
    ]
    assert [row["expert_type"] for row in history_store.written_rows["expert_opinions"]] == [
        "market_structure",
        "risk",
        "event_filter",
    ]


def test_governor_runtime_skips_duplicate_schedule_publication_when_snapshot_is_semantically_unchanged(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    now = datetime.now(UTC)

    class _Runner:
        async def run(self, state_summary):
            generated_at = datetime.now(UTC)
            return {
                "version_id": "model-generated-id",
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "effective_from": generated_at.isoformat().replace("+00:00", "Z"),
                "expires_at": (generated_at + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)

    runtime = governor_app.build_governor_runtime()
    runtime.last_snapshot = runtime.last_snapshot.model_copy(
        update={
            "version_id": "existing-snapshot",
            "generated_at": now - timedelta(seconds=15),
            "effective_from": now - timedelta(seconds=15),
            "expires_at": now + timedelta(minutes=3),
            "symbol_whitelist": ["BTC-USDT-SWAP"],
            "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
            "risk_multiplier": 0.5,
            "per_symbol_max_position": 0.12,
            "max_leverage": 3,
            "market_mode": RunMode.NORMAL,
            "approval_state": ApprovalState.APPROVED,
            "source_reason": "cycle|guardrailed",
            "ttl_sec": 300,
        }
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.last_snapshot.version_id == "existing-snapshot"
    assert history_store.written_rows["strategy_snapshots"] == []
    assert history_store.written_rows["governor_runs"] == [
        {
            "version_id": "existing-snapshot",
            "status": "unchanged",
            "error": None,
            "research_provider": "api",
            "research_status": "missing_symbol_summaries",
            "research_provider_success": None,
            "research_error": None,
            "research_candidate_count": 0,
            "approved_research_candidate_ids": [],
            "validation_status": "not_requested",
            "validation_error": None,
            "approval_status": "not_requested",
            "approval_error": None,
            "backtest_report_id": None,
            "approval_record_id": None,
        }
    ]


def test_governor_runtime_enriches_state_summary_with_qdrant_cases(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    captured_state_summary = None
    seen_queries: list[dict[str, object]] = []

    class _Runner:
        async def run(self, state_summary):
            nonlocal captured_state_summary
            captured_state_summary = state_summary
            now = datetime.now(UTC)
            return {
                "version_id": "snap-cases",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    class _CaseStore:
        def search_governance_cases(self, query: dict[str, object], limit: int = 3) -> list[dict[str, object]]:
            seen_queries.append(query)
            return [
                {
                    "case_id": "gov-001",
                    "summary": "manual takeover required after repeated recovery failures",
                    "recommended_mode": "halted",
                }
            ]

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_case_store", lambda settings: _CaseStore())

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_run_mode(RunMode.DEGRADED)
    runtime.history_store.append_risk_event({"event_type": "runtime_mode_changed", "detail": "reduced risk"})
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert seen_queries == [
        {
            "trigger_reason": "risk_event",
            "current_run_mode": "degraded",
            "recommended_mode_floor": "degraded",
            "active_fault_flags": [],
        }
    ]
    assert captured_state_summary["governance_cases"] == [
        {
            "case_id": "gov-001",
            "summary": "manual takeover required after repeated recovery failures",
            "recommended_mode": "halted",
        }
    ]


def test_governor_runtime_publishes_health_summary_and_trigger_reason(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-health",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.history_store.append_risk_event({"event_type": "runtime_mode_changed", "detail": "reduced risk"})
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.runtime_store.get_governor_health_summary() == {
        "status": "published",
        "trigger": "risk_event",
        "snapshot_version": runtime.last_snapshot.version_id,
        "market_mode": "degraded",
        "approval_state": "approved",
        "risk_multiplier": 0.25,
        "consecutive_failures": 0,
        "health_state": "healthy",
    }


def test_governor_runtime_logs_cycle_completion(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    logged: list[tuple[str, dict[str, object]]] = []

    class _Logger:
        def info(self, event: str, *, extra: dict[str, object]) -> None:
            logged.append((event, extra))

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-log",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    monkeypatch.setattr(governor_app, "_LOGGER", _Logger())
    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))

    runtime = governor_app.build_governor_runtime()

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert logged == [
        (
            "cycle_completed",
            {
                "service": "governor",
                "trigger_reason": "schedule",
                "status": "unchanged",
                "error": None,
                "snapshot_version": runtime.last_snapshot.version_id,
                "market_mode": "normal",
                "research_status": "missing_symbol_summaries",
                "research_candidate_count": 0,
                "approved_research_candidate_count": 0,
                "consecutive_failures": 0,
            },
        )
    ]


def test_governor_loop_runs_multiple_cycles_on_schedule(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_GOVERNOR_INTERVAL_SEC", "5")
    _clear_unrelated_settings_env(monkeypatch)
    seen_state_summaries: list[dict[str, object]] = []

    class _Runner:
        async def run(self, state_summary):
            seen_state_summaries.append(dict(state_summary))
            cycle_number = len(seen_state_summaries)
            now = datetime.now(UTC)
            return {
                "version_id": f"snap-{cycle_number}",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "symbol_whitelist": ["BTC-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
                "risk_multiplier": 0.5 if cycle_number == 1 else 0.4,
                "per_symbol_max_position": 0.12,
                "max_leverage": 3,
                "market_mode": RunMode.NORMAL,
                "approval_state": ApprovalState.APPROVED,
                "source_reason": "cycle",
                "ttl_sec": 300,
            }

    waits: list[int] = []

    async def _fake_wait_for_next_cycle(delay_sec: int) -> None:
        waits.append(delay_sec)
        if len(waits) >= 2:
            raise RuntimeError("stop loop")

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "_wait_for_next_cycle", _fake_wait_for_next_cycle)

    runtime = governor_app.build_governor_runtime()

    with pytest.raises(RuntimeError, match="stop loop"):
        asyncio.run(governor_app._run_governor_loop(runtime))

    assert len(runtime.published_snapshots) == 0
    assert waits == [5, 5]


def test_governor_loop_short_circuits_wait_on_event_trigger(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_GOVERNOR_INTERVAL_SEC", "30")
    _clear_unrelated_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            version = f"snap-{state_summary['trigger_reason']}"
            now = datetime.now(UTC)
            return {
                "version_id": version,
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    waits: list[int] = []

    async def _fake_wait_for_next_cycle(delay_sec: int) -> None:
        waits.append(delay_sec)
        if len(waits) == 1:
            runtime.history_store.append_risk_event({"event_type": "runtime_mode_changed", "detail": "reduced risk"})
            return
        raise RuntimeError("stop loop")

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(governor_app, "_wait_for_next_cycle", _fake_wait_for_next_cycle)

    runtime = governor_app.build_governor_runtime()

    with pytest.raises(RuntimeError, match="stop loop"):
        asyncio.run(governor_app._run_governor_loop(runtime))

    assert len(runtime.published_snapshots) == 1
    assert runtime.published_snapshots[0].version_id.startswith("governor-")
    assert waits == [5, 0]


def test_governor_cycle_keeps_manual_release_target_after_publishing_release_snapshot(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-release",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "symbol_whitelist": ["BTC-USDT-SWAP"],
                "strategy_enable_flags": {"breakout": True, "mean_reversion": False, "risk_pause": True},
                "risk_multiplier": 0.5,
                "per_symbol_max_position": 0.12,
                "max_leverage": 2,
                "market_mode": RunMode.DEGRADED,
                "approval_state": ApprovalState.APPROVED,
                "source_reason": "cycle",
                "ttl_sec": 300,
            }

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_manual_release_target("degraded")

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.runtime_store.get_manual_release_target() == "degraded"


def test_governor_cycle_freezes_snapshot_and_tracks_consecutive_failures(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")

    class _Runner:
        async def run(self, state_summary):
            raise RuntimeError("llm timeout")

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_run_mode(RunMode.DEGRADED)

    asyncio.run(governor_app._run_governor_cycle(runtime))
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.last_snapshot.version_id == "bootstrap"
    assert runtime.consecutive_failures == 2
    assert runtime.runtime_store.get_governor_health_summary() == {
        "status": "frozen",
        "trigger": "mode_change",
        "snapshot_version": "bootstrap",
        "market_mode": "normal",
        "approval_state": "approved",
        "risk_multiplier": 0.5,
        "consecutive_failures": 2,
        "health_state": "healthy",
    }
    assert history_store.written_rows["governor_runs"] == [
        {
            "version_id": "bootstrap",
            "status": "frozen",
            "error": "llm timeout",
            "research_provider": "api",
            "research_status": "missing_symbol_summaries",
            "research_provider_success": None,
            "research_error": None,
            "research_candidate_count": 0,
            "approved_research_candidate_ids": [],
            "validation_status": "not_requested",
            "validation_error": None,
            "approval_status": "not_requested",
            "approval_error": None,
            "backtest_report_id": None,
            "approval_record_id": None,
        },
        {
            "version_id": "bootstrap",
            "status": "frozen",
            "error": "llm timeout",
            "research_provider": "api",
            "research_status": "missing_symbol_summaries",
            "research_provider_success": None,
            "research_error": None,
            "research_candidate_count": 0,
            "approved_research_candidate_ids": [],
            "validation_status": "not_requested",
            "validation_error": None,
            "approval_status": "not_requested",
            "approval_error": None,
            "backtest_report_id": None,
            "approval_record_id": None,
        },
    ]


def test_governor_cycle_contains_research_provider_failure_to_research_branch(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    _clear_unrelated_settings_env(monkeypatch)
    history_store = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            now = datetime.now(UTC)
            return {
                "version_id": "snap-research-failure-contained",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "effective_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(governor_app, "build_history_store", lambda settings: history_store)
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    async def _failing_research_candidates(*_args, **_kwargs):
        raise RuntimeError("codex login required")

    monkeypatch.setattr(governor_app, "_prepare_research_candidates", _failing_research_candidates)
    monkeypatch.setattr(
        governor_app,
        "build_research_engine",
        lambda settings: StrategyResearchEngine(
            provider=type("Provider", (), {"provider_name": ResearchProviderName.CODEX_CLI})()
        ),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_symbol_runtime_summary(
        "BTC-USDT-SWAP",
        {"symbol": "BTC-USDT-SWAP", "mid_price": 100.0, "net_quantity": 0.0},
    )
    runtime.history_store.append_order_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:00:00Z", "price": 100.0}
    )
    runtime.history_store.append_position_fact(
        {"symbol": "BTC-USDT-SWAP", "generated_at": "2026-04-19T00:05:00Z", "mark_price": 103.0}
    )

    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.last_snapshot.version_id == "bootstrap"
    assert history_store.written_rows["governor_runs"] == [
        {
            "version_id": runtime.last_snapshot.version_id,
            "status": "unchanged",
            "error": "codex login required",
            "research_provider": "codex_cli",
            "research_status": "failed",
            "research_provider_success": False,
            "research_error": "codex login required",
            "research_candidate_count": 0,
            "approved_research_candidate_ids": [],
            "validation_status": "not_requested",
            "validation_error": None,
            "approval_status": "not_requested",
            "approval_error": None,
            "backtest_report_id": None,
            "approval_record_id": None,
        }
    ]


def test_governor_cycle_tightens_health_state_after_three_failures(monkeypatch) -> None:
    _set_required_settings_env(monkeypatch)
    fake_redis = _FakeRedis()

    class _Runner:
        async def run(self, state_summary):
            raise RuntimeError("llm timeout")

    monkeypatch.setattr(governor_app, "build_governor_client", lambda settings: GovernorClient(_Runner()))
    monkeypatch.setattr(
        governor_app,
        "build_runtime_state_store",
        lambda settings: governor_app.RedisRuntimeStateStore(redis_client=fake_redis),
    )

    runtime = governor_app.build_governor_runtime()
    runtime.runtime_store.set_run_mode(RunMode.DEGRADED)

    asyncio.run(governor_app._run_governor_cycle(runtime))
    asyncio.run(governor_app._run_governor_cycle(runtime))
    asyncio.run(governor_app._run_governor_cycle(runtime))

    assert runtime.consecutive_failures == 3
    assert runtime.runtime_store.get_governor_health_summary() == {
        "status": "frozen",
        "trigger": "mode_change",
        "snapshot_version": "bootstrap",
        "market_mode": "degraded",
        "approval_state": "approved",
        "risk_multiplier": 0.5,
        "consecutive_failures": 3,
        "health_state": "degraded",
    }

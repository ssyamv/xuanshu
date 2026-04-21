from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from xuanshu.config.settings import GovernorRuntimeSettings, Settings, TraderRuntimeSettings
from xuanshu.contracts.checkpoint import CheckpointBudgetState, CheckpointOrder, CheckpointPosition, ExecutionCheckpoint
from xuanshu.contracts.research import ResearchTrigger, StrategyPackage
from xuanshu.contracts.risk import CandidateSignal
from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.contracts.governance import ExpertOpinion
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import EntryType, MarketRegime, OkxAccountMode, OrderSide, RunMode, SignalUrgency, VolatilityState


def _sample_strategy_definition() -> dict[str, object]:
    return {
        "strategy_def_id": "strat-contract-001",
        "symbol": "BTC-USDT-SWAP",
        "strategy_family": "breakout",
        "directionality": "long_only",
        "feature_spec": {"indicators": [{"name": "sma", "source": "close", "window": 20}]},
        "entry_rules": {"all": [{"op": "crosses_above", "left": "close", "right": "sma_20"}]},
        "exit_rules": {"any": [{"op": "crosses_below", "left": "close", "right": "sma_20"}]},
        "position_sizing_rules": {"risk_fraction": 0.01},
        "risk_constraints": {"max_hold_minutes": 240},
        "parameter_set": {"lookback_fast": 20, "lookback_slow": 60},
        "score": 67.5,
        "score_basis": "backtest_return_percent",
    }


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


def test_strategy_package_contract_is_typed() -> None:
    package = StrategyPackage(
        strategy_package_id="pkg-001",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="long_only",
        entry_rules={"signal": "breakout_confirmed"},
        exit_rules={"stop_loss_bps": 50, "take_profit_bps": 120},
        position_sizing_rules={"risk_fraction": 0.0025},
        risk_constraints={"max_hold_minutes": 60},
        parameter_set={"lookback_fast": 20, "lookback_slow": 60},
        backtest_summary={"total_return": 0.18},
        performance_summary={"sharpe": 1.4},
        failure_modes=["range_whipsaw"],
        invalidating_conditions=["liquidity_collapse"],
        research_reason="manual research run",
        strategy_definition=_sample_strategy_definition(),
        score=67.5,
        score_basis="backtest_return_percent",
    )

    assert package.directionality == "long_only"


def test_strategy_package_rejects_unknown_trigger_value() -> None:
    payload = {
        "strategy_package_id": "pkg-001",
        "generated_at": datetime.now(UTC),
        "trigger": "not-a-trigger",
        "symbol_scope": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        "market_environment_scope": ["trend"],
        "strategy_family": "breakout",
        "directionality": "long_only",
        "entry_rules": {"signal": "breakout_confirmed"},
        "exit_rules": {"stop_loss_bps": 50, "take_profit_bps": 120},
        "position_sizing_rules": {"risk_fraction": 0.0025},
        "risk_constraints": {"max_hold_minutes": 60},
        "parameter_set": {"lookback_fast": 20, "lookback_slow": 60},
        "backtest_summary": {"total_return": 0.18},
        "performance_summary": {"sharpe": 1.4},
        "failure_modes": ["range_whipsaw"],
        "invalidating_conditions": ["liquidity_collapse"],
        "research_reason": "manual research run",
        "strategy_definition": _sample_strategy_definition(),
        "score": 67.5,
        "score_basis": "backtest_return_percent",
    }

    with pytest.raises(ValidationError):
        StrategyPackage(**payload)


@pytest.mark.parametrize(
    "field_name",
    ["symbol_scope", "market_environment_scope"],
)
def test_strategy_package_rejects_blank_entries(field_name: str) -> None:
    payload = {
        "strategy_package_id": "pkg-001",
        "generated_at": datetime.now(UTC),
        "trigger": ResearchTrigger.MANUAL,
        "symbol_scope": ["BTC-USDT-SWAP"],
        "market_environment_scope": ["trend"],
        "strategy_family": "breakout",
        "directionality": "long_only",
        "entry_rules": {"signal": "breakout_confirmed"},
        "exit_rules": {"stop_loss_bps": 50, "take_profit_bps": 120},
        "position_sizing_rules": {"risk_fraction": 0.0025},
        "risk_constraints": {"max_hold_minutes": 60},
        "parameter_set": {"lookback_fast": 20, "lookback_slow": 60},
        "backtest_summary": {"total_return": 0.18},
        "performance_summary": {"sharpe": 1.4},
        "failure_modes": ["range_whipsaw"],
        "invalidating_conditions": ["liquidity_collapse"],
        "research_reason": "manual research run",
        "strategy_definition": _sample_strategy_definition(),
        "score": 67.5,
        "score_basis": "backtest_return_percent",
    }
    payload[field_name] = ["", "trend"] if field_name == "market_environment_scope" else ["BTC-USDT-SWAP", " "]

    with pytest.raises(ValidationError):
        StrategyPackage(**payload)


def test_strategy_package_rejects_naive_generated_at() -> None:
    payload = {
        "strategy_package_id": "pkg-001",
        "generated_at": datetime.now(),
        "trigger": ResearchTrigger.MANUAL,
        "symbol_scope": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        "market_environment_scope": ["trend"],
        "strategy_family": "breakout",
        "directionality": "long_only",
        "entry_rules": {"signal": "breakout_confirmed"},
        "exit_rules": {"stop_loss_bps": 50, "take_profit_bps": 120},
        "position_sizing_rules": {"risk_fraction": 0.0025},
        "risk_constraints": {"max_hold_minutes": 60},
        "parameter_set": {"lookback_fast": 20, "lookback_slow": 60},
        "backtest_summary": {"total_return": 0.18},
        "performance_summary": {"sharpe": 1.4},
        "failure_modes": ["range_whipsaw"],
        "invalidating_conditions": ["liquidity_collapse"],
        "research_reason": "manual research run",
        "strategy_definition": _sample_strategy_definition(),
        "score": 67.5,
        "score_basis": "backtest_return_percent",
    }

    with pytest.raises(ValidationError, match="timezone-aware"):
        StrategyPackage(**payload)


def test_strategy_package_normalizes_generated_at_to_utc() -> None:
    package = StrategyPackage(
        strategy_package_id="pkg-001",
        generated_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=8))),
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="long_only",
        entry_rules={"signal": "breakout_confirmed"},
        exit_rules={"stop_loss_bps": 50, "take_profit_bps": 120},
        position_sizing_rules={"risk_fraction": 0.0025},
        risk_constraints={"max_hold_minutes": 60},
        parameter_set={"lookback_fast": 20, "lookback_slow": 60},
        backtest_summary={"total_return": 0.18},
        performance_summary={"sharpe": 1.4},
        failure_modes=["range_whipsaw"],
        invalidating_conditions=["liquidity_collapse"],
        research_reason="manual research run",
        strategy_definition=_sample_strategy_definition(),
        score=67.5,
        score_basis="backtest_return_percent",
    )

    assert package.generated_at == datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
    assert package.generated_at.tzinfo == UTC


@pytest.mark.parametrize("field_name", ["generated_at", "effective_from", "expires_at"])
def test_strategy_snapshot_rejects_naive_datetimes(field_name: str) -> None:
    payload = {
        "version_id": "snap-001",
        "generated_at": datetime.now(UTC),
        "effective_from": datetime.now(UTC),
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "symbol_whitelist": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        "strategy_enable_flags": {"breakout": True},
        "risk_multiplier": 0.8,
        "per_symbol_max_position": 0.12,
        "max_leverage": 3,
        "market_mode": RunMode.NORMAL,
        "approval_state": "approved",
        "source_reason": "committee result",
        "ttl_sec": 300,
    }
    payload[field_name] = datetime.now()

    with pytest.raises(ValidationError, match="timezone-aware"):
        StrategyConfigSnapshot(**payload)


def test_strategy_snapshot_rejects_naive_reference_times() -> None:
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

    with pytest.raises(ValueError, match="timezone-aware"):
        snapshot.is_active(datetime.now())


def test_governor_runtime_settings_allow_codex_cli_without_openai_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XUANSHU_RESEARCH_PROVIDER", "codex_cli")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql+psycopg://xuanshu:xuanshu@localhost:5432/xuanshu")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    settings = GovernorRuntimeSettings()

    assert settings.research_provider.value == "codex_cli"
    assert settings.openai_api_key is None


@pytest.mark.parametrize(
    "symbol_whitelist",
    [
        [""],
        ["BTC-USDT-SWAP", " "],
    ],
)
def test_strategy_snapshot_rejects_blank_symbol_whitelist_entries(symbol_whitelist: list[str]) -> None:
    payload = {
        "version_id": "snap-001",
        "generated_at": datetime.now(UTC),
        "effective_from": datetime.now(UTC),
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "symbol_whitelist": symbol_whitelist,
        "strategy_enable_flags": {"breakout": True},
        "risk_multiplier": 0.8,
        "per_symbol_max_position": 0.12,
        "max_leverage": 3,
        "market_mode": RunMode.NORMAL,
        "approval_state": "approved",
        "source_reason": "committee result",
        "ttl_sec": 300,
    }

    with pytest.raises(ValidationError):
        StrategyConfigSnapshot(**payload)


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("BTC-USDT-SWAP,ETH-USDT-SWAP", ("BTC-USDT-SWAP", "ETH-USDT-SWAP")),
        ("BTC-USDT-SWAP, ETH-USDT-SWAP", ("BTC-USDT-SWAP", "ETH-USDT-SWAP")),
    ],
)
def test_settings_load_okx_symbols_from_csv_env(monkeypatch: pytest.MonkeyPatch, env_value: str, expected: tuple[str, ...]) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("XUANSHU_OKX_SYMBOLS", env_value)

    settings = Settings()

    assert settings.okx_symbols == expected


def test_trader_runtime_settings_load_default_run_mode_from_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKX_API_KEY", "okx-key")
    monkeypatch.setenv("OKX_API_SECRET", "okx-secret")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "okx-passphrase")
    monkeypatch.setenv("XUANSHU_DEFAULT_RUN_MODE", "halted")

    settings = TraderRuntimeSettings()

    assert settings.default_run_mode == RunMode.HALTED


def test_trader_runtime_settings_load_okx_account_mode_from_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKX_API_KEY", "okx-key")
    monkeypatch.setenv("OKX_API_SECRET", "okx-secret")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "okx-passphrase")
    monkeypatch.setenv("XUANSHU_OKX_ACCOUNT_MODE", "demo")

    settings = TraderRuntimeSettings()

    assert settings.okx_account_mode == OkxAccountMode.DEMO


def test_trader_runtime_settings_default_okx_account_mode_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKX_API_KEY", "okx-key")
    monkeypatch.setenv("OKX_API_SECRET", "okx-secret")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "okx-passphrase")
    monkeypatch.delenv("XUANSHU_OKX_ACCOUNT_MODE", raising=False)

    settings = TraderRuntimeSettings()

    assert settings.okx_account_mode == OkxAccountMode.LIVE


@pytest.mark.parametrize(
    "settings_type",
    [Settings, TraderRuntimeSettings],
)
def test_runtime_settings_reject_blank_okx_symbols(
    settings_type: type[Settings] | type[TraderRuntimeSettings],
) -> None:
    payload = {
        "okx_symbols": [" "],
        "REDIS_URL": "redis://localhost:6379/0",
        "POSTGRES_DSN": "postgresql://xuanshu:xuanshu@localhost:5432/xuanshu",
        "QDRANT_URL": "http://localhost:6333",
        "OKX_API_KEY": "okx-key",
        "OKX_API_SECRET": "okx-secret",
        "OKX_API_PASSPHRASE": "okx-passphrase",
    }

    if settings_type is Settings:
        with pytest.raises(ValidationError):
            settings_type.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            settings_type.model_validate(
                {
                    "okx_symbols": [" "],
                    "OKX_API_KEY": "okx-key",
                    "OKX_API_SECRET": "okx-secret",
                    "OKX_API_PASSPHRASE": "okx-passphrase",
                }
            )


@pytest.mark.parametrize(
    ("field_name", "empty_value"),
    [
        ("opinion_id", ""),
        ("expert_type", ""),
        ("symbol_scope", []),
        ("decision", ""),
    ],
)
def test_expert_opinion_rejects_empty_key_fields(field_name: str, empty_value: object) -> None:
    payload = {
        "opinion_id": "op-001",
        "expert_type": "risk",
        "generated_at": datetime.now(UTC),
        "symbol_scope": ["BTC-USDT-SWAP"],
        "decision": "tighten_risk",
        "confidence": 0.8,
        "supporting_facts": ["recent risk events rising"],
        "risk_flags": ["drawdown_watch"],
        "ttl_sec": 300,
    }
    payload[field_name] = empty_value

    with pytest.raises(ValidationError):
        ExpertOpinion(**payload)


@pytest.mark.parametrize(
    "symbol_scope",
    [
        [""],
        ["BTC-USDT-SWAP", " "],
    ],
)
def test_expert_opinion_rejects_blank_symbol_scope_entries(symbol_scope: list[str]) -> None:
    payload = {
        "opinion_id": "op-001",
        "expert_type": "risk",
        "generated_at": datetime.now(UTC),
        "symbol_scope": symbol_scope,
        "decision": "tighten_risk",
        "confidence": 0.8,
        "supporting_facts": ["recent risk events rising"],
        "risk_flags": ["drawdown_watch"],
        "ttl_sec": 300,
    }

    with pytest.raises(ValidationError):
        ExpertOpinion(**payload)


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

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from xuanshu.config.settings import Settings, TraderRuntimeSettings
from xuanshu.contracts.checkpoint import CheckpointBudgetState, CheckpointOrder, CheckpointPosition, ExecutionCheckpoint
from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.contracts.risk import CandidateSignal
from xuanshu.contracts.strategy import ApprovedStrategyBinding, StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, EntryType, MarketRegime, OkxAccountMode, OrderSide, RunMode, SignalUrgency, StrategyId, VolatilityState


def test_strategy_snapshot_contract_is_typed_and_active_when_approved() -> None:
    snapshot = StrategyConfigSnapshot(
        version_id="snap-001",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["ETH-USDT-SWAP"],
        strategy_enable_flags={"vol_breakout": True},
        risk_multiplier=0.8,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="fixed strategy",
        ttl_sec=300,
        symbol_strategy_bindings={
            "ETH-USDT-SWAP": ApprovedStrategyBinding(
                strategy_def_id="vol-breakout-eth-4h",
                strategy_package_id="fixed-vol-breakout",
                backtest_report_id="bt-vol-breakout",
                score=56.04,
                score_basis="backtest_return_percent",
                approval_record_id="fixed-vol-breakout",
                activated_at=datetime.now(UTC),
            )
        },
    )

    assert snapshot.is_active(datetime.now(UTC)) is True
    assert snapshot.allows_symbol(" ETH-USDT-SWAP ")
    assert snapshot.is_strategy_enabled("vol_breakout")


def test_strategy_snapshot_supports_symbol_and_strategy_specific_bindings() -> None:
    now = datetime.now(UTC)
    short_binding = ApprovedStrategyBinding(
        strategy_def_id="short-momentum-btc-4h",
        strategy_package_id="fixed-short-momentum-btc-4h",
        backtest_report_id="bt-short-momentum-btc-4h",
        score=75.62,
        score_basis="backtest_return_percent",
        approval_record_id="fixed-short-momentum-btc-4h",
        activated_at=now,
    )
    snapshot = StrategyConfigSnapshot(
        version_id="snap-001",
        generated_at=now,
        effective_from=now,
        expires_at=now + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"vol_breakout": True, "short_momentum": True},
        risk_multiplier=0.8,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="fixed strategy",
        ttl_sec=300,
        strategy_bindings={"BTC-USDT-SWAP:short_momentum": short_binding},
    )

    assert snapshot.strategy_binding_for("BTC-USDT-SWAP", "short_momentum") == short_binding


@pytest.mark.parametrize("field_name", ["generated_at", "effective_from", "expires_at"])
def test_strategy_snapshot_rejects_naive_datetimes(field_name: str) -> None:
    payload = {
        "version_id": "snap-001",
        "generated_at": datetime.now(UTC),
        "effective_from": datetime.now(UTC),
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "symbol_whitelist": ["ETH-USDT-SWAP"],
        "strategy_enable_flags": {"vol_breakout": True},
        "risk_multiplier": 0.8,
        "per_symbol_max_position": 0.12,
        "max_leverage": 3,
        "market_mode": RunMode.NORMAL,
        "approval_state": ApprovalState.APPROVED,
        "source_reason": "fixed strategy",
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
        symbol_whitelist=["ETH-USDT-SWAP"],
        strategy_enable_flags={"vol_breakout": True},
        risk_multiplier=0.8,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="fixed strategy",
        ttl_sec=300,
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        snapshot.is_active(datetime.now())


@pytest.mark.parametrize(
    "symbol_whitelist",
    [
        [""],
        ["ETH-USDT-SWAP", " "],
    ],
)
def test_strategy_snapshot_rejects_blank_symbol_whitelist_entries(symbol_whitelist: list[str]) -> None:
    payload = {
        "version_id": "snap-001",
        "generated_at": datetime.now(UTC),
        "effective_from": datetime.now(UTC),
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "symbol_whitelist": symbol_whitelist,
        "strategy_enable_flags": {"vol_breakout": True},
        "risk_multiplier": 0.8,
        "per_symbol_max_position": 0.12,
        "max_leverage": 3,
        "market_mode": RunMode.NORMAL,
        "approval_state": ApprovalState.APPROVED,
        "source_reason": "fixed strategy",
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
    if settings_type is Settings:
        with pytest.raises(ValidationError):
            settings_type.model_validate(
                {
                    "okx_symbols": [" "],
                    "REDIS_URL": "redis://localhost:6379/0",
                    "POSTGRES_DSN": "postgresql://xuanshu:xuanshu@localhost:5432/xuanshu",
                }
            )
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


def test_taxonomy_and_numeric_bounds_reject_invalid_contracts() -> None:
    with pytest.raises(ValidationError):
        CandidateSignal(
            symbol="ETH-USDT-SWAP",
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
            symbol="ETH-USDT-SWAP",
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
                symbol="ETH-USDT-SWAP",
                net_quantity=0.2,
                mark_price=3200.0,
                unrealized_pnl=125.0,
            )
        ],
        open_orders_snapshot=[
            CheckpointOrder(
                order_id="order-001",
                symbol="ETH-USDT-SWAP",
                side=OrderSide.BUY,
                price=3210.0,
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

    assert checkpoint.positions_snapshot[0].symbol == "ETH-USDT-SWAP"

    with pytest.raises(ValidationError):
        ExecutionCheckpoint(
            checkpoint_id="cp-002",
            created_at=datetime.now(UTC),
            active_snapshot_version="snap-002",
            current_mode=RunMode.NORMAL,
            positions_snapshot=[{"symbol": "ETH-USDT-SWAP", "net_quantity": 0.2}],
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
        )

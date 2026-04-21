from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from xuanshu.contracts.research import StrategyPackage
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.contracts.strategy_definition import StrategyDefinition


def _sample_strategy_definition() -> dict[str, object]:
    return {
        "strategy_def_id": "strat-btc-001",
        "symbol": "BTC-USDT-SWAP",
        "strategy_family": "volatility_break_retest",
        "directionality": "long_only",
        "feature_spec": {
            "indicators": [
                {"name": "sma", "source": "close", "window": 20},
                {"name": "ema", "source": "close", "window": 50},
            ]
        },
        "entry_rules": {
            "all": [
                {"op": "crosses_above", "left": "close", "right": "sma_20"},
                {"op": "greater_than", "left": "sma_20", "right": "ema_50"},
            ]
        },
        "exit_rules": {
            "any": [
                {"op": "crosses_below", "left": "close", "right": "sma_20"},
                {"op": "take_profit_bps", "value": 900},
                {"op": "stop_loss_bps", "value": 300},
            ]
        },
        "position_sizing_rules": {"risk_fraction": 0.01},
        "risk_constraints": {"max_hold_minutes": 240},
        "parameter_set": {"fast_window": 20, "slow_window": 50},
        "score": 67.5,
        "score_basis": "backtest_return_percent",
    }


def test_strategy_definition_accepts_supported_dsl_shape() -> None:
    definition = StrategyDefinition.model_validate(_sample_strategy_definition())

    assert definition.symbol == "BTC-USDT-SWAP"
    assert definition.score == 67.5
    assert definition.entry_rules["all"][0]["op"] == "crosses_above"


def test_strategy_definition_rejects_unsupported_operator() -> None:
    payload = _sample_strategy_definition()
    payload["entry_rules"] = {"all": [{"op": "exec_python", "value": "boom"}]}

    with pytest.raises(ValidationError, match="unsupported operator"):
        StrategyDefinition.model_validate(payload)


def test_strategy_package_requires_embedded_strategy_definition() -> None:
    package = StrategyPackage.model_validate(
        {
            "strategy_package_id": "pkg-1",
            "generated_at": datetime.now(UTC),
            "trigger": "schedule",
            "symbol_scope": ["BTC-USDT-SWAP"],
            "market_environment_scope": ["trend"],
            "strategy_family": "volatility_break_retest",
            "directionality": "long_only",
            "entry_rules": {"signal": "dsl"},
            "exit_rules": {"mode": "dsl"},
            "position_sizing_rules": {"risk_fraction": 0.01},
            "risk_constraints": {"max_hold_minutes": 240},
            "parameter_set": {"fast_window": 20},
            "backtest_summary": {"row_count": 100},
            "performance_summary": {"return_percent": 67.5},
            "failure_modes": ["late_reversal"],
            "invalidating_conditions": ["gap_down"],
            "research_reason": "ai candidate",
            "strategy_definition": _sample_strategy_definition(),
            "score": 67.5,
            "score_basis": "backtest_return_percent",
        }
    )

    assert package.strategy_definition.strategy_def_id == "strat-btc-001"


def test_strategy_package_rejects_missing_strategy_definition() -> None:
    payload = {
        "strategy_package_id": "pkg-1",
        "generated_at": datetime.now(UTC),
        "trigger": "schedule",
        "symbol_scope": ["BTC-USDT-SWAP"],
        "market_environment_scope": ["trend"],
        "strategy_family": "volatility_break_retest",
        "directionality": "long_only",
        "entry_rules": {"signal": "dsl"},
        "exit_rules": {"mode": "dsl"},
        "position_sizing_rules": {"risk_fraction": 0.01},
        "risk_constraints": {"max_hold_minutes": 240},
        "parameter_set": {"fast_window": 20},
        "backtest_summary": {"row_count": 100},
        "performance_summary": {"return_percent": 67.5},
        "failure_modes": ["late_reversal"],
        "invalidating_conditions": ["gap_down"],
        "research_reason": "ai candidate",
        "score": 67.5,
        "score_basis": "backtest_return_percent",
    }

    with pytest.raises(ValidationError, match="strategy_definition"):
        StrategyPackage.model_validate(payload)


def test_strategy_snapshot_accepts_symbol_strategy_bindings() -> None:
    snapshot = StrategyConfigSnapshot.model_validate(
        {
            "version_id": "snap-1",
            "generated_at": "2026-04-21T00:00:00Z",
            "effective_from": "2026-04-21T00:00:00Z",
            "expires_at": "2026-04-21T00:05:00Z",
            "symbol_whitelist": ["BTC-USDT-SWAP"],
            "strategy_enable_flags": {"risk_pause": True},
            "risk_multiplier": 0.5,
            "per_symbol_max_position": 0.12,
            "max_leverage": 3,
            "market_mode": "normal",
            "approval_state": "approved",
            "source_reason": "approved research package",
            "ttl_sec": 300,
            "symbol_strategy_bindings": {
                "BTC-USDT-SWAP": {
                    "strategy_def_id": "strat-btc-001",
                    "strategy_package_id": "pkg-1",
                    "backtest_report_id": "bt-1",
                    "score": 67.5,
                    "score_basis": "backtest_return_percent",
                    "approval_record_id": "apr-1",
                    "activated_at": "2026-04-21T00:00:00Z",
                }
            },
        }
    )

    assert snapshot.symbol_strategy_bindings["BTC-USDT-SWAP"].score == 67.5

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
        "parameter_set": {"fast_window": 20},
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


def test_strategy_definition_rejects_negative_score() -> None:
    payload = _sample_strategy_definition()
    payload["score"] = -1.0

    with pytest.raises(ValidationError):
        StrategyDefinition.model_validate(payload)


@pytest.mark.parametrize(
    ("field_name", "bad_value", "message"),
    [
        ("feature_spec", {"indicators": [{"name": "vwap", "source": "close", "window": 20}]}, "unsupported indicator"),
        ("feature_spec", {"indicators": [{"name": "sma", "source": "bid", "window": 20}]}, "unsupported source"),
        ("directionality", "long_short", "unsupported directionality"),
        ("score_basis", "sharpe_ratio", "unsupported score basis"),
    ],
)
def test_strategy_definition_rejects_unsupported_dsl_values(field_name: str, bad_value: object, message: str) -> None:
    payload = _sample_strategy_definition()
    payload[field_name] = bad_value

    with pytest.raises(ValidationError, match=message):
        StrategyDefinition.model_validate(payload)


def test_strategy_definition_rejects_missing_comparison_operand() -> None:
    payload = _sample_strategy_definition()
    payload["entry_rules"] = {"all": [{"op": "greater_than", "left": "close"}]}

    with pytest.raises(ValidationError, match="comparison nodes must contain exactly op, left, and right"):
        StrategyDefinition.model_validate(payload)


@pytest.mark.parametrize(
    ("op", "value"),
    [("take_profit_bps", -1), ("stop_loss_bps", 0), ("time_stop_minutes", -10)],
)
def test_strategy_definition_rejects_non_positive_exit_values(op: str, value: int) -> None:
    payload = _sample_strategy_definition()
    payload["exit_rules"] = {"any": [{"op": op, "value": value}]}

    with pytest.raises(ValidationError, match="must be positive"):
        StrategyDefinition.model_validate(payload)


def test_strategy_package_requires_embedded_strategy_definition() -> None:
    definition = _sample_strategy_definition()
    package = StrategyPackage.model_validate(
        {
            "strategy_package_id": "pkg-1",
            "generated_at": datetime.now(UTC),
            "trigger": "schedule",
            "symbol_scope": ["BTC-USDT-SWAP"],
            "market_environment_scope": ["trend"],
            "strategy_family": "volatility_break_retest",
            "directionality": "long_only",
            "entry_rules": definition["entry_rules"],
            "exit_rules": definition["exit_rules"],
            "position_sizing_rules": definition["position_sizing_rules"],
            "risk_constraints": definition["risk_constraints"],
            "parameter_set": definition["parameter_set"],
            "backtest_summary": {"row_count": 100},
            "performance_summary": {"return_percent": 67.5},
            "failure_modes": ["late_reversal"],
            "invalidating_conditions": ["gap_down"],
            "research_reason": "ai candidate",
            "strategy_definition": definition,
            "score": 67.5,
            "score_basis": "backtest_return_percent",
        }
    )

    assert package.strategy_definition.strategy_def_id == "strat-btc-001"


def test_strategy_package_rejects_inconsistent_embedded_definition() -> None:
    definition = _sample_strategy_definition()
    payload = {
        "strategy_package_id": "pkg-1",
        "generated_at": datetime.now(UTC),
        "trigger": "schedule",
        "symbol_scope": ["BTC-USDT-SWAP"],
        "market_environment_scope": ["trend"],
        "strategy_family": "volatility_break_retest",
        "directionality": "long_only",
        "entry_rules": definition["entry_rules"],
        "exit_rules": definition["exit_rules"],
        "position_sizing_rules": definition["position_sizing_rules"],
        "risk_constraints": definition["risk_constraints"],
        "parameter_set": {"fast_window": 99},
        "backtest_summary": {"row_count": 100},
        "performance_summary": {"return_percent": 67.5},
        "failure_modes": ["late_reversal"],
        "invalidating_conditions": ["gap_down"],
        "research_reason": "ai candidate",
        "strategy_definition": definition,
        "score": 67.5,
        "score_basis": "backtest_return_percent",
    }

    with pytest.raises(ValidationError, match="parameter_set must match"):
        StrategyPackage.model_validate(payload)


def test_strategy_package_rejects_missing_strategy_definition() -> None:
    definition = _sample_strategy_definition()
    payload = {
        "strategy_package_id": "pkg-1",
        "generated_at": datetime.now(UTC),
        "trigger": "schedule",
        "symbol_scope": ["BTC-USDT-SWAP"],
        "market_environment_scope": ["trend"],
        "strategy_family": "volatility_break_retest",
        "directionality": "long_only",
        "entry_rules": definition["entry_rules"],
        "exit_rules": definition["exit_rules"],
        "position_sizing_rules": definition["position_sizing_rules"],
        "risk_constraints": definition["risk_constraints"],
        "parameter_set": definition["parameter_set"],
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


def test_strategy_package_rejects_negative_score() -> None:
    definition = _sample_strategy_definition()
    payload = {
        "strategy_package_id": "pkg-1",
        "generated_at": datetime.now(UTC),
        "trigger": "schedule",
        "symbol_scope": ["BTC-USDT-SWAP"],
        "market_environment_scope": ["trend"],
        "strategy_family": "volatility_break_retest",
        "directionality": "long_only",
        "entry_rules": definition["entry_rules"],
        "exit_rules": definition["exit_rules"],
        "position_sizing_rules": definition["position_sizing_rules"],
        "risk_constraints": definition["risk_constraints"],
        "parameter_set": definition["parameter_set"],
        "backtest_summary": {"row_count": 100},
        "performance_summary": {"return_percent": 67.5},
        "failure_modes": ["late_reversal"],
        "invalidating_conditions": ["gap_down"],
        "research_reason": "ai candidate",
        "strategy_definition": definition,
        "score": -1.0,
        "score_basis": "backtest_return_percent",
    }

    with pytest.raises(ValidationError):
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


def test_strategy_package_rejects_entry_rule_mismatch() -> None:
    definition = _sample_strategy_definition()
    payload = {
        "strategy_package_id": "pkg-1",
        "generated_at": datetime.now(UTC),
        "trigger": "schedule",
        "symbol_scope": ["BTC-USDT-SWAP"],
        "market_environment_scope": ["trend"],
        "strategy_family": "volatility_break_retest",
        "directionality": "long_only",
        "entry_rules": {"all": [{"op": "crosses_below", "left": "close", "right": "sma_20"}]},
        "exit_rules": definition["exit_rules"],
        "position_sizing_rules": definition["position_sizing_rules"],
        "risk_constraints": definition["risk_constraints"],
        "parameter_set": definition["parameter_set"],
        "backtest_summary": {"row_count": 100},
        "performance_summary": {"return_percent": 67.5},
        "failure_modes": ["late_reversal"],
        "invalidating_conditions": ["gap_down"],
        "research_reason": "ai candidate",
        "strategy_definition": definition,
        "score": 67.5,
        "score_basis": "backtest_return_percent",
    }

    with pytest.raises(ValidationError, match="entry_rules must match"):
        StrategyPackage.model_validate(payload)


def test_strategy_package_rejects_risk_constraints_mismatch() -> None:
    definition = _sample_strategy_definition()
    payload = {
        "strategy_package_id": "pkg-1",
        "generated_at": datetime.now(UTC),
        "trigger": "schedule",
        "symbol_scope": ["BTC-USDT-SWAP"],
        "market_environment_scope": ["trend"],
        "strategy_family": "volatility_break_retest",
        "directionality": "long_only",
        "entry_rules": definition["entry_rules"],
        "exit_rules": definition["exit_rules"],
        "position_sizing_rules": definition["position_sizing_rules"],
        "risk_constraints": {"max_hold_minutes": 999},
        "parameter_set": definition["parameter_set"],
        "backtest_summary": {"row_count": 100},
        "performance_summary": {"return_percent": 67.5},
        "failure_modes": ["late_reversal"],
        "invalidating_conditions": ["gap_down"],
        "research_reason": "ai candidate",
        "strategy_definition": definition,
        "score": 67.5,
        "score_basis": "backtest_return_percent",
    }

    with pytest.raises(ValidationError, match="risk_constraints must match"):
        StrategyPackage.model_validate(payload)


def test_strategy_definition_rejects_combinator_shape_conflicts() -> None:
    payload = _sample_strategy_definition()
    payload["entry_rules"] = {"all": [], "any": []}

    with pytest.raises(ValidationError, match="combinator nodes must contain exactly one of all or any"):
        StrategyDefinition.model_validate(payload)


def test_strategy_definition_rejects_combinator_and_operator_conflict() -> None:
    payload = _sample_strategy_definition()
    payload["entry_rules"] = {"all": [{"op": "crosses_above", "left": "close", "right": "sma_20"}], "op": "greater_than"}

    with pytest.raises(ValidationError, match="combinator nodes must contain exactly one of all or any"):
        StrategyDefinition.model_validate(payload)


@pytest.mark.parametrize(
    ("field_name", "rule_payload", "message"),
    [
        (
            "entry_rules",
            {"all": [{"op": "crosses_above", "left": "close", "right": "sma_20", "extra": True}]},
            "comparison nodes must contain exactly op, left, and right",
        ),
        (
            "exit_rules",
            {"any": [{"op": "take_profit_bps", "value": 900, "extra": True}]},
            "exit primitive nodes must contain exactly op and value",
        ),
    ],
)
def test_strategy_definition_rejects_extra_rule_node_keys(
    field_name: str,
    rule_payload: dict[str, object],
    message: str,
) -> None:
    payload = _sample_strategy_definition()
    payload[field_name] = rule_payload

    with pytest.raises(ValidationError, match=message):
        StrategyDefinition.model_validate(payload)


def test_strategy_binding_rejects_whitespace_identifiers() -> None:
    with pytest.raises(ValidationError):
        StrategyConfigSnapshot.model_validate(
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
                        "strategy_def_id": "   ",
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


def test_strategy_binding_rejects_unsupported_score_basis() -> None:
    with pytest.raises(ValidationError, match="unsupported score basis"):
        StrategyConfigSnapshot.model_validate(
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
                        "score_basis": "sharpe_ratio",
                        "approval_record_id": "apr-1",
                        "activated_at": "2026-04-21T00:00:00Z",
                    }
                },
            }
        )


def test_strategy_snapshot_rejects_blank_symbol_strategy_binding_key() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        StrategyConfigSnapshot.model_validate(
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
                    "   ": {
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


def test_strategy_snapshot_rejects_symbol_strategy_binding_key_not_in_whitelist() -> None:
    with pytest.raises(ValidationError, match="must be listed in symbol_whitelist"):
        StrategyConfigSnapshot.model_validate(
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
                    "ETH-USDT-SWAP": {
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

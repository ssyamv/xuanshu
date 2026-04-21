from datetime import UTC, datetime, timedelta

import pytest

from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.contracts.strategy_definition import StrategyDefinition
from xuanshu.core.enums import MarketRegime, OrderSide, RunMode, StrategyId, VolatilityState
from xuanshu.strategies.dsl_execution import build_candidate_signal
from xuanshu.strategies.dsl_features import build_feature_context
from xuanshu.strategies.dsl_rules import evaluate_rule_tree
from xuanshu.strategies.signals import build_candidate_signals


def _sample_strategy_definition(*, directionality: str, entry_rule: dict[str, object], strategy_family: str) -> StrategyDefinition:
    return StrategyDefinition.model_validate(
        {
            "strategy_def_id": f"{strategy_family}-001",
            "symbol": "BTC-USDT-SWAP",
            "strategy_family": strategy_family,
            "directionality": directionality,
            "feature_spec": {"indicators": [{"name": "sma", "source": "close", "window": 3}]},
            "entry_rules": entry_rule,
            "exit_rules": {
                "any": [
                    {"op": "crosses_below", "left": "close", "right": "sma_3"},
                    {"op": "take_profit_bps", "value": 250},
                    {"op": "stop_loss_bps", "value": 100},
                    {"op": "time_stop_minutes", "value": 15},
                ]
            },
            "position_sizing_rules": {"risk_fraction": 0.01},
            "risk_constraints": {"max_hold_minutes": 15},
            "parameter_set": {"lookback": 3},
            "score": 42.0,
            "score_basis": "backtest_return_percent",
        }
    )


def _rows(values: list[float], *, start: datetime | None = None) -> list[dict[str, object]]:
    start = start or datetime(2026, 4, 21, 0, 0, tzinfo=UTC)
    return [
        {
            "timestamp": start + timedelta(minutes=index),
            "close": value,
        }
        for index, value in enumerate(values)
    ]


def _snapshot() -> MarketStateSnapshot:
    return MarketStateSnapshot(
        snapshot_id="snap-001",
        generated_at=datetime(2026, 4, 21, 0, 0, tzinfo=UTC),
        symbol="BTC-USDT-SWAP",
        mid_price=100.0,
        spread=0.1,
        imbalance=0.0,
        recent_trade_bias=0.0,
        volatility_state=VolatilityState.NORMAL,
        regime=MarketRegime.UNKNOWN,
        current_position=0.0,
        current_mode=RunMode.NORMAL,
        risk_budget_remaining=1.0,
    )


def test_feature_computation_and_rule_evaluation_returns_true_when_close_greater_than_sma_3() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
    )
    context = build_feature_context(definition, _rows([10.0, 11.0, 12.0, 13.0]))

    assert evaluate_rule_tree(definition.entry_rules, context) is True
    assert context.current_features["sma_3"] == pytest.approx(12.0)


def test_entry_rule_false_path_returns_no_signal() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
    )

    signal = build_candidate_signal(definition, _rows([10.0, 11.0, 12.0, 11.0]))

    assert signal is None


def test_long_only_definition_produces_buy_signal() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
    )
    signals = build_candidate_signals(
        _snapshot(),
        dsl_strategy_definitions=[definition],
        historical_rows_by_strategy_def_id={definition.strategy_def_id: _rows([10.0, 11.0, 12.0, 13.0])},
    )

    assert len(signals) == 1
    assert signals[0].side == OrderSide.BUY
    assert signals[0].strategy_id == StrategyId.BREAKOUT


def test_short_only_definition_produces_sell_signal() -> None:
    definition = _sample_strategy_definition(
        directionality="short_only",
        strategy_family="mean_reversion",
        entry_rule={"all": [{"op": "less_than", "left": "close", "right": "sma_3"}]},
    )
    signals = build_candidate_signals(
        _snapshot(),
        dsl_strategy_definitions=[definition],
        historical_rows_by_strategy_def_id={definition.strategy_def_id: _rows([13.0, 12.0, 11.0, 10.0])},
    )

    assert len(signals) == 1
    assert signals[0].side == OrderSide.SELL
    assert signals[0].strategy_id == StrategyId.MEAN_REVERSION


def test_feature_context_raises_value_error_when_rows_are_insufficient() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
    )

    with pytest.raises(ValueError, match="historical_rows must contain at least"):
        build_feature_context(definition, _rows([10.0, 11.0]))

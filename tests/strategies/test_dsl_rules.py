from datetime import UTC, datetime, timedelta

import pytest

from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.contracts.strategy_definition import StrategyDefinition
from xuanshu.core.enums import MarketRegime, OrderSide, RunMode, StrategyId, VolatilityState
from xuanshu.strategies.dsl_execution import build_candidate_signal
from xuanshu.strategies.dsl_features import build_feature_context
from xuanshu.strategies.dsl_rules import evaluate_rule_tree
from xuanshu.strategies.signals import build_candidate_signals


def _sample_strategy_definition(
    *,
    directionality: str,
    entry_rule: dict[str, object],
    strategy_family: str,
    include_time_stop: bool = True,
    feature_indicators: list[dict[str, object]] | None = None,
    risk_constraints: dict[str, object] | None = None,
) -> StrategyDefinition:
    exit_children: list[dict[str, object]] = [
        {"op": "crosses_below", "left": "close", "right": "sma_3"},
        {"op": "take_profit_bps", "value": 250},
        {"op": "stop_loss_bps", "value": 100},
    ]
    if include_time_stop:
        exit_children.append({"op": "time_stop_minutes", "value": 15})
    return StrategyDefinition.model_validate(
        {
            "strategy_def_id": f"{strategy_family}-001",
            "symbol": "BTC-USDT-SWAP",
            "strategy_family": strategy_family,
            "directionality": directionality,
            "feature_spec": {
                "indicators": feature_indicators
                if feature_indicators is not None
                else [{"name": "sma", "source": "close", "window": 3}]
            },
            "entry_rules": entry_rule,
            "exit_rules": {"any": exit_children},
            "position_sizing_rules": {"risk_fraction": 0.01},
            "risk_constraints": risk_constraints if risk_constraints is not None else {"max_hold_minutes": 15},
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


def _trend_snapshot() -> MarketStateSnapshot:
    snapshot = _snapshot()
    return snapshot.model_copy(update={"regime": MarketRegime.TREND})


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
    assert signals[0].risk_tag == "dsl:breakout:breakout-001"


def test_definition_without_time_stop_uses_risk_constraints_for_max_hold() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        include_time_stop=False,
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
        risk_constraints={"max_hold_minutes": 7},
    )

    signal = build_candidate_signal(definition, _rows([10.0, 11.0, 12.0, 13.0]))

    assert signal is not None
    assert signal.max_hold_ms == 7 * 60_000
    assert signal.risk_tag == "dsl:breakout:breakout-001"


def test_time_stop_exit_rule_lookup_normalizes_operator_text() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        include_time_stop=False,
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
        risk_constraints={"max_hold_minutes": 7},
    )
    exit_rules = definition.exit_rules.model_copy() if hasattr(definition.exit_rules, "model_copy") else dict(definition.exit_rules)
    exit_rules["any"] = [
        *exit_rules["any"],
        {"op": " Time_Stop_Minutes ", "value": 3},
    ]
    definition = definition.model_copy(update={"exit_rules": exit_rules})

    signal = build_candidate_signal(definition, _rows([10.0, 11.0, 12.0, 13.0]))

    assert signal is not None
    assert signal.max_hold_ms == 3 * 60_000


def test_empty_dsl_definition_list_falls_back_to_legacy_behavior() -> None:
    signals = build_candidate_signals(
        _trend_snapshot(),
        dsl_strategy_definitions=[],
        historical_rows_by_strategy_def_id={},
    )

    assert len(signals) == 1
    assert signals[0].strategy_id == StrategyId.BREAKOUT
    assert signals[0].risk_tag == "trend"
    assert signals[0].side == OrderSide.BUY


def test_missing_rows_for_one_dsl_definition_skips_only_that_definition() -> None:
    match_definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
    )
    missing_definition = _sample_strategy_definition(
        directionality="short_only",
        strategy_family="mean_reversion",
        entry_rule={"all": [{"op": "less_than", "left": "close", "right": "sma_3"}]},
    )

    signals = build_candidate_signals(
        _snapshot(),
        dsl_strategy_definitions=[match_definition, missing_definition],
        historical_rows_by_strategy_def_id={
            match_definition.strategy_def_id: _rows([10.0, 11.0, 12.0, 13.0]),
        },
    )

    assert len(signals) == 1
    assert signals[0].strategy_id == StrategyId.BREAKOUT
    assert signals[0].risk_tag == "dsl:breakout:breakout-001"


def test_missing_rows_for_all_dsl_definitions_return_no_signal() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
    )

    signals = build_candidate_signals(
        _trend_snapshot(),
        dsl_strategy_definitions=[definition],
        historical_rows_by_strategy_def_id={},
    )

    assert signals == []


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
    assert signals[0].risk_tag == "dsl:mean_reversion:mean_reversion-001"


def test_feature_context_raises_value_error_when_rows_are_insufficient() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
    )

    with pytest.raises(ValueError, match="historical_rows must contain at least"):
        build_feature_context(definition, _rows([10.0, 11.0]))


def test_feature_context_raises_on_duplicate_feature_names() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
        feature_indicators=[
            {"name": "sma", "source": "close", "window": 3},
            {"name": "sma", "source": "volume", "window": 3},
        ],
    )

    with pytest.raises(ValueError, match="duplicate feature name: sma_3"):
        build_feature_context(
            definition,
            [
                {"timestamp": datetime(2026, 4, 21, 0, 0, tzinfo=UTC), "close": 10.0, "volume": 100.0},
                {"timestamp": datetime(2026, 4, 21, 0, 1, tzinfo=UTC), "close": 11.0, "volume": 110.0},
                {"timestamp": datetime(2026, 4, 21, 0, 2, tzinfo=UTC), "close": 12.0, "volume": 120.0},
                {"timestamp": datetime(2026, 4, 21, 0, 3, tzinfo=UTC), "close": 13.0, "volume": 130.0},
            ],
        )


def test_atr_uses_previous_close_before_window_for_first_true_range() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "atr_3"}]},
        include_time_stop=False,
        feature_indicators=[{"name": "atr", "source": "close", "window": 3}],
        risk_constraints={"max_hold_minutes": 7},
    )
    context = build_feature_context(
        definition,
        [
            {"timestamp": datetime(2026, 4, 21, 0, 0, tzinfo=UTC), "high": 11.0, "low": 9.0, "close": 10.0},
            {"timestamp": datetime(2026, 4, 21, 0, 1, tzinfo=UTC), "high": 21.0, "low": 19.0, "close": 20.0},
            {"timestamp": datetime(2026, 4, 21, 0, 2, tzinfo=UTC), "high": 22.0, "low": 20.0, "close": 21.0},
            {"timestamp": datetime(2026, 4, 21, 0, 3, tzinfo=UTC), "high": 23.0, "low": 21.0, "close": 22.0},
        ],
    )

    assert context.current_features["atr_3"] == pytest.approx(5.0)


def test_rule_evaluator_supports_any_const_and_crosses_above() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        entry_rule={
            "any": [
                {"op": "greater_than", "left": "close", "right": {"const": 20}},
                {"op": "crosses_above", "left": "close", "right": "sma_3"},
            ]
        },
    )
    context = build_feature_context(definition, _rows([10.0, 9.0, 8.0, 13.0]))

    assert evaluate_rule_tree(definition.entry_rules, context) is True


def test_rule_evaluator_rejects_extra_keys_in_combinator_node() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
    )
    context = build_feature_context(definition, _rows([10.0, 11.0, 12.0, 13.0]))

    with pytest.raises(ValueError, match="combinator nodes must contain exactly one of all or any"):
        evaluate_rule_tree({"all": definition.entry_rules["all"], "op": "greater_than"}, context)


def test_rule_evaluator_rejects_extra_keys_in_operator_node() -> None:
    definition = _sample_strategy_definition(
        directionality="long_only",
        strategy_family="breakout",
        entry_rule={"all": [{"op": "greater_than", "left": "close", "right": "sma_3"}]},
    )
    context = build_feature_context(definition, _rows([10.0, 11.0, 12.0, 13.0]))

    with pytest.raises(ValueError, match="comparison nodes must contain exactly op, left, and right"):
        evaluate_rule_tree({"op": "greater_than", "left": "close", "right": "sma_3", "extra": True}, context)

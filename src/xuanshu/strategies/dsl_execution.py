from __future__ import annotations

from collections.abc import Sequence

from xuanshu.contracts.risk import CandidateSignal
from xuanshu.contracts.strategy_definition import StrategyDefinition
from xuanshu.core.enums import EntryType, OrderSide, SignalUrgency, StrategyId
from xuanshu.strategies.dsl_features import build_feature_context
from xuanshu.strategies.dsl_rules import evaluate_rule_tree


def build_candidate_signal(
    strategy_definition: StrategyDefinition,
    historical_rows: Sequence[dict[str, object]],
) -> CandidateSignal | None:
    feature_context = build_feature_context(strategy_definition, historical_rows)
    if not evaluate_rule_tree(strategy_definition.entry_rules, feature_context):
        return None

    strategy_id = _resolve_strategy_id(strategy_definition)
    side = OrderSide.BUY if strategy_definition.directionality == "long_only" else OrderSide.SELL
    max_hold_ms = _extract_max_hold_ms(strategy_definition)
    cancel_after_ms = max(1, min(max_hold_ms, 5_000))

    return CandidateSignal(
        symbol=strategy_definition.symbol,
        strategy_id=strategy_id,
        side=side,
        entry_type=EntryType.MARKET,
        urgency=SignalUrgency.NORMAL,
        confidence=_normalized_confidence(strategy_definition.score),
        max_hold_ms=max_hold_ms,
        cancel_after_ms=cancel_after_ms,
        risk_tag=f"dsl:{strategy_definition.strategy_family}:{strategy_definition.strategy_def_id}",
    )


def _resolve_strategy_id(strategy_definition: StrategyDefinition) -> StrategyId:
    try:
        return StrategyId(strategy_definition.strategy_family)
    except ValueError:
        if strategy_definition.directionality == "long_only":
            return StrategyId.BREAKOUT
        return StrategyId.MEAN_REVERSION


def _normalized_confidence(score: float) -> float:
    return max(0.0, min(1.0, score / 100.0))


def _extract_max_hold_ms(strategy_definition: StrategyDefinition) -> int:
    minutes = _find_exit_rule_value(strategy_definition.exit_rules, "time_stop_minutes")
    if minutes is not None:
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            raise ValueError("time_stop_minutes value must be a positive integer")
        return minutes * 60_000

    max_hold_minutes = strategy_definition.risk_constraints.get("max_hold_minutes")
    if isinstance(max_hold_minutes, bool) or not isinstance(max_hold_minutes, int) or max_hold_minutes <= 0:
        raise ValueError("risk_constraints.max_hold_minutes must be a positive integer when time_stop_minutes is absent")
    return max_hold_minutes * 60_000


def _find_exit_rule_value(node: object, key: str) -> object | None:
    if isinstance(node, dict):
        op = node.get("op")
        if isinstance(op, str) and op.strip().lower() == key:
            return node.get("value")
        for child_key in ("all", "any"):
            children = node.get(child_key)
            if isinstance(children, list):
                for child in children:
                    found = _find_exit_rule_value(child, key)
                    if found is not None:
                        return found
    return None

from __future__ import annotations

from collections.abc import Mapping
from numbers import Real

from xuanshu.strategies.dsl_features import FeatureContext


def evaluate_rule_tree(rule_tree: Mapping[str, object], context: FeatureContext) -> bool:
    if not isinstance(rule_tree, Mapping):
        raise ValueError("rule node must be a mapping")
    if "all" in rule_tree:
        children = rule_tree["all"]
        if not isinstance(children, list) or not children:
            raise ValueError("all must contain rule nodes")
        return all(evaluate_rule_tree(child, context) for child in children)
    if "any" in rule_tree:
        children = rule_tree["any"]
        if not isinstance(children, list) or not children:
            raise ValueError("any must contain rule nodes")
        return any(evaluate_rule_tree(child, context) for child in children)

    op = _normalize_op(rule_tree)
    left = _resolve_operand(rule_tree.get("left"), context)
    right = _resolve_right_operand(rule_tree.get("right"), context)

    if op == "greater_than":
        return left > right
    if op == "less_than":
        return left < right
    if op == "crosses_above":
        previous_left = _resolve_operand(rule_tree.get("left"), context, previous=True)
        previous_right = _resolve_right_operand(rule_tree.get("right"), context, previous=True)
        return previous_left <= previous_right and left > right
    if op == "crosses_below":
        previous_left = _resolve_operand(rule_tree.get("left"), context, previous=True)
        previous_right = _resolve_right_operand(rule_tree.get("right"), context, previous=True)
        return previous_left >= previous_right and left < right

    raise ValueError("unsupported operator in entry rule evaluation")


def _normalize_op(rule_tree: Mapping[str, object]) -> str:
    op = rule_tree.get("op")
    if not isinstance(op, str) or not op.strip():
        raise ValueError("rule node must contain op")
    return op.strip().lower()


def _resolve_operand(value: object, context: FeatureContext, *, previous: bool = False) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("rule operands must be non-empty strings")
    normalized = value.strip()
    features = context.previous_features if previous else context.current_features
    row = context.previous_row if previous else context.current_row
    if normalized in features:
        return features[normalized]
    if normalized in row:
        row_value = row[normalized]
        if isinstance(row_value, bool) or not isinstance(row_value, Real):
            raise ValueError(f"{normalized} must resolve to a real number")
        return float(row_value)
    raise ValueError(f"unknown rule operand: {normalized}")


def _resolve_right_operand(value: object, context: FeatureContext, *, previous: bool = False) -> float:
    if isinstance(value, Mapping):
        if set(value.keys()) != {"const"}:
            raise ValueError("const mappings must contain exactly const")
        const_value = value["const"]
        if isinstance(const_value, bool) or not isinstance(const_value, Real):
            raise ValueError("const values must be numeric")
        return float(const_value)
    return _resolve_operand(value, context, previous=previous)

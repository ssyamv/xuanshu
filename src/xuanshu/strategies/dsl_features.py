from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from numbers import Real
import math

from xuanshu.contracts.strategy_definition import IndicatorSpec, StrategyDefinition


@dataclass(frozen=True, slots=True)
class FeatureContext:
    current_row: dict[str, object]
    previous_row: dict[str, object]
    current_features: dict[str, float]
    previous_features: dict[str, float]
    row_count: int


def build_feature_context(
    strategy_definition: StrategyDefinition,
    historical_rows: Sequence[Mapping[str, object]],
) -> FeatureContext:
    normalized_rows = _normalize_rows(historical_rows)
    indicators = _extract_indicators(strategy_definition)
    required_row_count = _required_row_count(indicators)
    if len(normalized_rows) < required_row_count:
        raise ValueError(f"historical_rows must contain at least {required_row_count} rows")

    current_row = normalized_rows[-1]
    previous_row = normalized_rows[-2]
    current_features = _build_features_for_rows(normalized_rows, indicators, previous=False)
    previous_features = _build_features_for_rows(normalized_rows, indicators, previous=True)

    return FeatureContext(
        current_row=current_row,
        previous_row=previous_row,
        current_features=current_features,
        previous_features=previous_features,
        row_count=len(normalized_rows),
    )


def _extract_indicators(strategy_definition: StrategyDefinition) -> list[IndicatorSpec]:
    indicators = strategy_definition.feature_spec.get("indicators", [])
    if not isinstance(indicators, list):
        raise ValueError("feature_spec.indicators must be a list")
    return [IndicatorSpec.model_validate(indicator) for indicator in indicators]


def _required_row_count(indicators: Sequence[IndicatorSpec]) -> int:
    windows = [indicator.window for indicator in indicators if indicator.window is not None]
    if not windows:
        raise ValueError("feature_spec.indicators must define a window")
    return max(windows) + 1


def _normalize_rows(historical_rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    if not historical_rows:
        raise ValueError("historical_rows must not be empty")

    normalized_rows = [_normalize_row(row) for row in historical_rows]
    normalized_rows.sort(key=lambda item: item["timestamp"])
    for previous, current in zip(normalized_rows, normalized_rows[1:], strict=False):
        if previous["timestamp"] == current["timestamp"]:
            raise ValueError("historical_rows timestamps must be unique")
    return normalized_rows


def _normalize_row(row: Mapping[str, object]) -> dict[str, object]:
    timestamp = row.get("timestamp")
    if not isinstance(timestamp, datetime):
        raise ValueError("historical_rows timestamp values must be datetimes")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("historical_rows timestamp values must be timezone-aware")

    normalized_row: dict[str, object] = {"timestamp": timestamp.astimezone(UTC)}
    for key, value in row.items():
        if key == "timestamp":
            continue
        normalized_row[key] = value
    return normalized_row


def _build_features_for_rows(
    historical_rows: Sequence[Mapping[str, object]],
    indicators: Sequence[IndicatorSpec],
    *,
    previous: bool,
) -> dict[str, float]:
    features: dict[str, float] = {}
    for indicator in indicators:
        window = indicator.window
        if window is None:
            raise ValueError("feature_spec.indicators must define a window")
        row_slice = _indicator_row_slice(historical_rows, window=window, previous=previous)
        feature_name = _indicator_name(indicator)
        if feature_name in features:
            raise ValueError(f"duplicate feature name: {feature_name}")
        value = _compute_indicator(indicator, row_slice, historical_rows=historical_rows, previous=previous)
        features[feature_name] = value
    base_row = historical_rows[-2 if previous else -1]
    if "close" in base_row:
        features["close"] = _coerce_number(base_row["close"], field_name="close")
    return features


def _indicator_row_slice(
    historical_rows: Sequence[Mapping[str, object]],
    *,
    window: int,
    previous: bool,
) -> Sequence[Mapping[str, object]]:
    if previous:
        return historical_rows[-window - 1 : -1]
    return historical_rows[-window:]


def _compute_indicator(
    indicator: IndicatorSpec,
    rows: Sequence[Mapping[str, object]],
    *,
    historical_rows: Sequence[Mapping[str, object]],
    previous: bool,
) -> float:
    source = indicator.source or "close"
    try:
        values = [_coerce_number(row[source], field_name=source) for row in rows]
    except KeyError as exc:
        raise ValueError(f"historical_rows must include {source} values for {indicator.name}") from exc
    if indicator.name == "sma":
        return sum(values) / len(values)
    if indicator.name == "ema":
        return _compute_ema(values)
    if indicator.name == "highest":
        return max(values)
    if indicator.name == "lowest":
        return min(values)
    if indicator.name == "zscore":
        return _compute_zscore(values)
    if indicator.name == "atr":
        start_index = len(historical_rows) - len(rows) - (1 if previous else 0)
        prior_close = None
        if start_index > 0:
            prior_close = _coerce_number(historical_rows[start_index - 1]["close"], field_name="close")
        return _compute_atr(rows, prior_close=prior_close)
    raise ValueError("unsupported indicator")


def _compute_ema(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("indicator values must not be empty")
    alpha = 2.0 / (len(values) + 1.0)
    ema = values[0]
    for value in values[1:]:
        ema = alpha * value + (1.0 - alpha) * ema
    return ema


def _compute_zscore(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("indicator values must not be empty")
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    if variance == 0.0:
        return 0.0
    return (values[-1] - mean) / math.sqrt(variance)


def _compute_atr(rows: Sequence[Mapping[str, object]], *, prior_close: float | None) -> float:
    true_ranges: list[float] = []
    previous_close: float | None = prior_close
    for row in rows:
        try:
            high = _coerce_number(row["high"], field_name="high")
            low = _coerce_number(row["low"], field_name="low")
            close = _coerce_number(row["close"], field_name="close")
        except KeyError as exc:
            raise ValueError("historical_rows must include high, low, and close values for atr") from exc
        range_components = [high - low]
        if previous_close is not None:
            range_components.extend([abs(high - previous_close), abs(low - previous_close)])
        true_ranges.append(max(range_components))
        previous_close = close
    return sum(true_ranges) / len(true_ranges)


def _coerce_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real | Decimal):
        raise ValueError(f"historical_rows {field_name} values must be real numbers")
    try:
        normalized_value = float(value)
    except OverflowError as exc:
        raise ValueError(f"historical_rows {field_name} values must be finite") from exc
    if not math.isfinite(normalized_value):
        raise ValueError(f"historical_rows {field_name} values must be finite")
    return normalized_value


def _indicator_name(indicator: IndicatorSpec) -> str:
    if indicator.window is None:
        return indicator.name
    return f"{indicator.name}_{indicator.window}"

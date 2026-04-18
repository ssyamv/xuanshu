from __future__ import annotations

import re


_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")
_STRATEGY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _validate_component(label: str, value: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {label}: {value!r}")
    if value != value.strip():
        raise ValueError(f"invalid {label}: {value!r}")
    if not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def build_client_order_id(symbol: str, strategy_id: str, sequence: int) -> str:
    _validate_component("symbol", symbol, _SYMBOL_PATTERN)
    _validate_component("strategy_id", strategy_id, _STRATEGY_PATTERN)
    if type(sequence) is not int or sequence < 0 or sequence > 999_999:
        raise ValueError(f"invalid sequence: {sequence!r}")
    return f"{symbol}-{strategy_id}-{sequence:06d}"

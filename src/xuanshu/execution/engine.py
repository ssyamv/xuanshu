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


def build_market_order_payload(symbol: str, side: str, size: float, client_order_id: str) -> dict[str, str]:
    _validate_component("symbol", symbol, _SYMBOL_PATTERN)
    if side not in {"buy", "sell"}:
        raise ValueError(f"invalid side: {side!r}")
    if not isinstance(size, int | float) or isinstance(size, bool):
        raise ValueError(f"invalid size: {size!r}")
    if size <= 0:
        raise ValueError(f"invalid size: {size!r}")
    _validate_component("client_order_id", client_order_id, re.compile(r"^\S+$"))
    return {
        "instId": symbol,
        "tdMode": "cross",
        "side": side,
        "ordType": "market",
        "sz": f"{size:g}",
        "clOrdId": client_order_id,
    }

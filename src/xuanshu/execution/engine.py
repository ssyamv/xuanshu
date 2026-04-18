from __future__ import annotations


def build_client_order_id(symbol: str, strategy_id: str, sequence: int) -> str:
    return f"{symbol}-{strategy_id}-{sequence:06d}"

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Protocol

from xuanshu.contracts.checkpoint import ExecutionCheckpoint

_ORDER_FIELDS = ("order_id", "symbol", "side", "price", "size", "status")
_POSITION_FIELDS = ("symbol", "net_quantity", "mark_price", "unrealized_pnl")
_ORDER_FIELD_ALIASES = {
    "order_id": "ordId",
    "symbol": "instId",
    "side": "side",
    "price": "px",
    "size": "sz",
    "status": "state",
}
_POSITION_FIELD_ALIASES = {
    "symbol": "instId",
    "net_quantity": "pos",
    "mark_price": "markPx",
    "unrealized_pnl": "upl",
}


class RecoveryRestClient(Protocol):
    async def fetch_open_orders(self, symbol: str, timestamp: str) -> list[dict[str, object]]:
        ...

    async def fetch_positions(self, symbol: str, timestamp: str) -> list[dict[str, object]]:
        ...


@dataclass
class RecoverySupervisor:
    rest_client: RecoveryRestClient

    async def run_startup_recovery(
        self,
        symbol: str,
        checkpoint: ExecutionCheckpoint,
        timestamp: str,
    ) -> dict[str, object]:
        open_orders, positions = await asyncio.gather(
            self.rest_client.fetch_open_orders(symbol, timestamp),
            self.rest_client.fetch_positions(symbol, timestamp),
        )
        checkpoint_orders = _normalize_checkpoint_items(checkpoint.open_orders_snapshot, fields=_ORDER_FIELDS)
        exchange_orders = _normalize_exchange_items(open_orders, field_aliases=_ORDER_FIELD_ALIASES)
        checkpoint_positions = _normalize_checkpoint_items(checkpoint.positions_snapshot, fields=_POSITION_FIELDS)
        exchange_positions = _normalize_exchange_items(positions, field_aliases=_POSITION_FIELD_ALIASES)
        if checkpoint_orders != exchange_orders or checkpoint_positions != exchange_positions:
            return {
                "run_mode": "halted",
                "needs_reconcile": True,
                "reason": "exchange_state_mismatch",
            }
        return {
            "run_mode": checkpoint.current_mode.value,
            "needs_reconcile": False,
            "reason": "checkpoint_matches_exchange",
        }


def _normalize_checkpoint_items(items: list[object], *, fields: tuple[str, ...]) -> list[tuple[object, ...]]:
    return sorted(
        (tuple(getattr(item, field) for field in fields) for item in items),
        key=_item_sort_key,
    )


def _normalize_exchange_items(
    items: list[dict[str, object]],
    *,
    field_aliases: dict[str, str],
) -> list[tuple[object, ...]]:
    normalized_items: list[tuple[object, ...]] = []
    for item in items:
        payload = item if isinstance(item, dict) else {}
        normalized_items.append(
            tuple(
                _normalize_exchange_value(field, payload.get(exchange_field))
                for field, exchange_field in field_aliases.items()
            )
        )
    return sorted(normalized_items, key=_item_sort_key)


def _item_sort_key(item: tuple[object, ...]) -> tuple[tuple[str, object], ...]:
    return tuple(_value_sort_key(value) for value in item)


def _value_sort_key(value: object) -> tuple[str, object]:
    if value is None:
        return ("none", "")
    if isinstance(value, float):
        if math.isnan(value):
            return ("nan", "")
        return ("float", value)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, str):
        return ("str", value)
    return ("repr", repr(value))


def _normalize_exchange_value(field: str, value: object) -> object:
    if field in {"price", "size", "net_quantity", "mark_price", "unrealized_pnl"}:
        return _coerce_float(value)
    return value


def _coerce_float(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return float("nan")
    return float("nan")

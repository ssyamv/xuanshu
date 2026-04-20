from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Protocol

from xuanshu.contracts.checkpoint import ExecutionCheckpoint

_ORDER_FIELDS = ("order_id", "symbol", "side", "price", "size", "status")
_POSITION_FIELDS = ("symbol", "net_quantity", "mark_price", "unrealized_pnl")
_POSITION_RECOVERY_FIELDS = ("symbol", "net_quantity")
_ORDER_FIELD_ALIASES = {
    "order_id": "ordId",
    "client_order_id": "clOrdId",
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
        try:
            open_orders, positions = await asyncio.gather(
                self.rest_client.fetch_open_orders(symbol, timestamp),
                self.rest_client.fetch_positions(symbol, timestamp),
            )
        except Exception:
            return {
                "run_mode": "halted",
                "needs_reconcile": True,
                "reason": "exchange_state_mismatch",
            }
        checkpoint_orders = _normalize_checkpoint_items(
            [item for item in checkpoint.open_orders_snapshot if getattr(item, "symbol", None) == symbol],
            fields=_ORDER_FIELDS,
        )
        exchange_orders = _normalize_exchange_items(open_orders, field_aliases=_ORDER_FIELD_ALIASES)
        checkpoint_positions = _normalize_checkpoint_items(
            [item for item in checkpoint.positions_snapshot if getattr(item, "symbol", None) == symbol],
            fields=_POSITION_RECOVERY_FIELDS,
        )
        exchange_positions = _normalize_exchange_items(
            positions,
            field_aliases=_POSITION_FIELD_ALIASES,
            fields=_POSITION_RECOVERY_FIELDS,
        )
        if not _orders_match(checkpoint_orders, exchange_orders) or checkpoint_positions != exchange_positions:
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
    fields: tuple[str, ...] | None = None,
) -> list[tuple[object, ...]]:
    normalized_items: list[tuple[object, ...]] = []
    selected_fields = fields or tuple(field_aliases.keys())
    for item in items:
        payload = item if isinstance(item, dict) else {}
        if field_aliases is _POSITION_FIELD_ALIASES and _is_flat_exchange_position_payload(payload):
            continue
        normalized_item = tuple(
            _normalize_exchange_value(field, payload.get(exchange_field))
            for field in selected_fields
            for exchange_field in (field_aliases[field],)
        )
        normalized_items.append(normalized_item)
    return sorted(normalized_items, key=_item_sort_key)


def _orders_match(
    checkpoint_orders: list[tuple[object, ...]],
    exchange_orders: list[tuple[object, ...]],
) -> bool:
    if len(checkpoint_orders) != len(exchange_orders):
        return False
    remaining_exchange = list(exchange_orders)
    for checkpoint_order in checkpoint_orders:
        for index, exchange_order in enumerate(remaining_exchange):
            if _checkpoint_order_matches_exchange_order(checkpoint_order, exchange_order):
                remaining_exchange.pop(index)
                break
        else:
            return False
    return not remaining_exchange


def _checkpoint_order_matches_exchange_order(
    checkpoint_order: tuple[object, ...],
    exchange_order: tuple[object, ...],
) -> bool:
    if len(checkpoint_order) != len(_ORDER_FIELDS):
        return False
    if len(exchange_order) != len(_ORDER_FIELD_ALIASES):
        return False

    checkpoint_order_id, checkpoint_symbol, checkpoint_side, checkpoint_price, checkpoint_size, checkpoint_status = (
        checkpoint_order
    )
    (
        exchange_order_id,
        exchange_client_order_id,
        exchange_symbol,
        exchange_side,
        exchange_price,
        exchange_size,
        exchange_status,
    ) = exchange_order

    if checkpoint_symbol != exchange_symbol or checkpoint_side != exchange_side:
        return False
    if checkpoint_size != exchange_size:
        return False
    if checkpoint_order_id not in {exchange_order_id, exchange_client_order_id}:
        return False
    if not _statuses_compatible(checkpoint_status, exchange_status):
        return False
    return _prices_compatible(checkpoint_price, exchange_price)


def _statuses_compatible(checkpoint_status: object, exchange_status: object) -> bool:
    if checkpoint_status == exchange_status:
        return True
    return {checkpoint_status, exchange_status} == {"submitted", "live"}


def _prices_compatible(checkpoint_price: object, exchange_price: object) -> bool:
    if checkpoint_price == exchange_price:
        return True
    if checkpoint_price == 0.0 and exchange_price in {None, 0.0}:
        return True
    return False


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
    if isinstance(value, bool):
        return float("nan")
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


def _is_flat_exchange_position_payload(payload: dict[str, object]) -> bool:
    net_quantity = _normalize_exchange_value("net_quantity", payload.get(_POSITION_FIELD_ALIASES["net_quantity"]))
    mark_price = _normalize_exchange_value("mark_price", payload.get(_POSITION_FIELD_ALIASES["mark_price"]))
    unrealized_pnl = _normalize_exchange_value(
        "unrealized_pnl",
        payload.get(_POSITION_FIELD_ALIASES["unrealized_pnl"]),
    )
    if not isinstance(net_quantity, float) or net_quantity != 0.0:
        return False
    return mark_price is None and unrealized_pnl is None

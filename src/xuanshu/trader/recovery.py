from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from xuanshu.contracts.checkpoint import ExecutionCheckpoint

_ORDER_FIELDS = ("order_id", "symbol", "side", "price", "size", "status")
_POSITION_FIELDS = ("symbol", "net_quantity", "mark_price", "unrealized_pnl")


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
        open_orders = await self.rest_client.fetch_open_orders(symbol, timestamp)
        positions = await self.rest_client.fetch_positions(symbol, timestamp)
        checkpoint_orders = _normalize_checkpoint_items(checkpoint.open_orders_snapshot, fields=_ORDER_FIELDS)
        exchange_orders = _normalize_exchange_items(open_orders, fields=_ORDER_FIELDS)
        checkpoint_positions = _normalize_checkpoint_items(checkpoint.positions_snapshot, fields=_POSITION_FIELDS)
        exchange_positions = _normalize_exchange_items(positions, fields=_POSITION_FIELDS)
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
    return sorted(tuple(getattr(item, field) for field in fields) for item in items)


def _normalize_exchange_items(
    items: list[dict[str, object]],
    *,
    fields: tuple[str, ...],
) -> list[tuple[object, ...]]:
    return sorted(tuple(item.get(field) for field in fields) for item in items)

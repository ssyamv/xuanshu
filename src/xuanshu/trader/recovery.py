from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from xuanshu.contracts.checkpoint import ExecutionCheckpoint


class RecoveryRestClient(Protocol):
    async def fetch_open_orders(self, symbol: str, timestamp: str) -> list[dict[str, object]]:
        ...

    async def fetch_positions(self, symbol: str, timestamp: str) -> list[dict[str, object]]:
        ...

    async def fetch_account_summary(self, timestamp: str) -> list[dict[str, object]]:
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
        await self.rest_client.fetch_account_summary(timestamp)
        checkpoint_orders = len(checkpoint.open_orders_snapshot)
        checkpoint_positions = len(checkpoint.positions_snapshot)
        if len(open_orders) != checkpoint_orders or len(positions) != checkpoint_positions:
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

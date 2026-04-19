from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from xuanshu.contracts.risk import RiskDecision
from xuanshu.execution.engine import build_market_order_payload


class ExecutionRestClient(Protocol):
    async def place_order(self, payload: dict[str, str], timestamp: str) -> list[dict[str, object]]:
        ...


@dataclass
class ExecutionCoordinator:
    rest_client: ExecutionRestClient
    inflight_by_client_order_id: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def submit_market_open(
        self,
        symbol: str,
        side: str,
        size: float,
        client_order_id: str,
        decision: RiskDecision,
        timestamp: str,
    ) -> list[dict[str, object]] | None:
        if client_order_id in self.inflight_by_client_order_id:
            entry = self.inflight_by_client_order_id[client_order_id]
            payload = build_market_order_payload(symbol, side, size, client_order_id)
            if entry.get("payload") != payload:
                raise ValueError(
                    f"client_order_id {client_order_id!r} collides with original order parameters"
                )
            task = entry.get("task")
            if task is not None:
                return await asyncio.shield(task)
            return entry["response"]
        if not decision.allow_open:
            return None
        payload = build_market_order_payload(symbol, side, size, client_order_id)
        task = asyncio.create_task(self.rest_client.place_order(payload, timestamp))
        entry = {
            "symbol": symbol,
            "payload": payload,
            "task": task,
        }
        self.inflight_by_client_order_id[client_order_id] = entry
        task.add_done_callback(
            lambda completed_task: self._finalize_inflight_entry(
                client_order_id,
                entry,
                completed_task,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise

    def _finalize_inflight_entry(
        self,
        client_order_id: str,
        entry: dict[str, Any],
        task: asyncio.Task[list[dict[str, object]]],
    ) -> None:
        if self.inflight_by_client_order_id.get(client_order_id) is not entry:
            return
        if task.cancelled():
            self.inflight_by_client_order_id.pop(client_order_id, None)
            return
        exception = task.exception()
        if exception is not None:
            self.inflight_by_client_order_id.pop(client_order_id, None)
            return
        entry["response"] = task.result()
        entry.pop("task", None)

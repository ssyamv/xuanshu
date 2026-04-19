from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from xuanshu.contracts.risk import RiskDecision
from xuanshu.execution.engine import build_market_order_payload


class ExecutionRestClient(Protocol):
    async def place_order(self, payload: dict[str, str], timestamp: str) -> dict[str, object]:
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
    ) -> dict[str, object] | None:
        if client_order_id in self.inflight_by_client_order_id:
            entry = self.inflight_by_client_order_id[client_order_id]
            task = entry.get("task")
            if task is not None:
                response = await task
                entry["response"] = response
                return response
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
        try:
            response = await task
        except Exception:
            if self.inflight_by_client_order_id.get(client_order_id) is entry:
                self.inflight_by_client_order_id.pop(client_order_id, None)
            raise
        entry["response"] = response
        entry.pop("task", None)
        return response

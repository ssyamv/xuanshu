from __future__ import annotations

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
            return self.inflight_by_client_order_id[client_order_id]["response"]
        if not decision.allow_open:
            return None
        payload = build_market_order_payload(symbol, side, size, client_order_id)
        response = await self.rest_client.place_order(payload, timestamp)
        self.inflight_by_client_order_id[client_order_id] = {
            "symbol": symbol,
            "payload": payload,
            "response": response,
        }
        return response

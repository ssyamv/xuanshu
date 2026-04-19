from datetime import UTC, datetime

import pytest

from xuanshu.contracts.risk import RiskDecision
from xuanshu.core.enums import RunMode
from xuanshu.execution.coordinator import ExecutionCoordinator


class _FakeRestClient:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, str], str]] = []

    async def place_order(self, payload: dict[str, str], timestamp: str) -> dict[str, object]:
        self.calls.append((payload, timestamp))
        return {"data": [{"ordId": "1", "clOrdId": payload["clOrdId"], "sCode": "0"}]}


@pytest.mark.asyncio
async def test_execution_coordinator_places_idempotent_order_once() -> None:
    rest = _FakeRestClient()
    coordinator = ExecutionCoordinator(rest_client=rest)
    decision = RiskDecision(
        decision_id="dec-1",
        generated_at=datetime.now(UTC),
        symbol="BTC-USDT-SWAP",
        allow_open=True,
        allow_close=True,
        max_position=100.0,
        max_order_size=1.0,
        risk_mode=RunMode.NORMAL,
        reason_codes=[],
    )

    await coordinator.submit_market_open(
        symbol="BTC-USDT-SWAP",
        side="buy",
        size=1.0,
        client_order_id="btc-breakout-000001",
        decision=decision,
        timestamp="2026-04-19T00:00:00.000Z",
    )
    await coordinator.submit_market_open(
        symbol="BTC-USDT-SWAP",
        side="buy",
        size=1.0,
        client_order_id="btc-breakout-000001",
        decision=decision,
        timestamp="2026-04-19T00:00:00.000Z",
    )

    assert len(rest.calls) == 1
    assert coordinator.inflight_by_client_order_id["btc-breakout-000001"]["symbol"] == "BTC-USDT-SWAP"

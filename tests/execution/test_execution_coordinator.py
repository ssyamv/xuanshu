import asyncio
from datetime import UTC, datetime

import pytest

from xuanshu.contracts.risk import RiskDecision
from xuanshu.core.enums import RunMode
from xuanshu.execution.coordinator import ExecutionCoordinator
from xuanshu.execution.engine import build_market_order_payload


class _FakeRestClient:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, str], str]] = []

    async def place_order(self, payload: dict[str, str], timestamp: str) -> list[dict[str, object]]:
        self.calls.append((payload, timestamp))
        return [{"ordId": "1", "clOrdId": payload["clOrdId"], "sCode": "0"}]


@pytest.mark.asyncio
async def test_execution_coordinator_returns_cached_open_even_if_later_decision_disallows_open() -> None:
    rest = _FakeRestClient()
    coordinator = ExecutionCoordinator(rest_client=rest)
    allow_open_decision = RiskDecision(
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
        decision=allow_open_decision,
        timestamp="2026-04-19T00:00:00.000Z",
    )
    cached_response = await coordinator.submit_market_open(
        symbol="BTC-USDT-SWAP",
        side="buy",
        size=1.0,
        client_order_id="btc-breakout-000001",
        decision=RiskDecision(
            decision_id="dec-2",
            generated_at=datetime.now(UTC),
            symbol="BTC-USDT-SWAP",
            allow_open=False,
            allow_close=True,
            max_position=100.0,
            max_order_size=1.0,
            risk_mode=RunMode.NORMAL,
            reason_codes=[],
        ),
        timestamp="2026-04-19T00:00:00.000Z",
    )

    assert cached_response == [{"ordId": "1", "clOrdId": "btc-breakout-000001", "sCode": "0"}]
    assert len(rest.calls) == 1
    assert coordinator.inflight_by_client_order_id["btc-breakout-000001"]["symbol"] == "BTC-USDT-SWAP"


def test_build_market_order_payload_rejects_blank_client_order_id() -> None:
    with pytest.raises(ValueError, match=r"invalid client_order_id"):
        build_market_order_payload("BTC-USDT-SWAP", "buy", 1.0, "   ")


def test_build_market_order_payload_rejects_non_string_side() -> None:
    with pytest.raises(ValueError, match=r"invalid side"):
        build_market_order_payload("BTC-USDT-SWAP", ["buy"], 1.0, "btc-breakout-000001")


class _BlockingRestClient:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, str], str]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.error: Exception | None = None

    async def place_order(self, payload: dict[str, str], timestamp: str) -> list[dict[str, object]]:
        self.calls.append((payload, timestamp))
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return [{"ordId": "1", "clOrdId": payload["clOrdId"], "sCode": "0"}]


@pytest.mark.asyncio
async def test_execution_coordinator_deduplicates_inflight_open_submission() -> None:
    rest = _BlockingRestClient()
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

    first = asyncio.create_task(
        coordinator.submit_market_open(
            symbol="BTC-USDT-SWAP",
            side="buy",
            size=1.0,
            client_order_id="btc-breakout-000002",
            decision=decision,
            timestamp="2026-04-19T00:00:00.000Z",
        )
    )
    await rest.started.wait()

    second = asyncio.create_task(
        coordinator.submit_market_open(
            symbol="BTC-USDT-SWAP",
            side="buy",
            size=1.0,
            client_order_id="btc-breakout-000002",
            decision=decision,
            timestamp="2026-04-19T00:00:00.000Z",
        )
    )
    await asyncio.sleep(0)

    assert len(rest.calls) == 1

    rest.release.set()
    first_response = await first
    second_response = await second

    assert first_response == second_response
    assert coordinator.inflight_by_client_order_id["btc-breakout-000002"]["response"] == first_response


@pytest.mark.asyncio
async def test_execution_coordinator_duplicate_waiter_cancellation_does_not_cancel_shared_task() -> None:
    rest = _BlockingRestClient()
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

    first = asyncio.create_task(
        coordinator.submit_market_open(
            symbol="BTC-USDT-SWAP",
            side="buy",
            size=1.0,
            client_order_id="btc-breakout-000004",
            decision=decision,
            timestamp="2026-04-19T00:00:00.000Z",
        )
    )
    await rest.started.wait()

    second = asyncio.create_task(
        coordinator.submit_market_open(
            symbol="BTC-USDT-SWAP",
            side="buy",
            size=1.0,
            client_order_id="btc-breakout-000004",
            decision=decision,
            timestamp="2026-04-19T00:00:00.000Z",
        )
    )
    await asyncio.sleep(0)
    second.cancel()

    with pytest.raises(asyncio.CancelledError):
        await second

    rest.release.set()
    response = await first

    assert response == [{"ordId": "1", "clOrdId": "btc-breakout-000004", "sCode": "0"}]
    assert len(rest.calls) == 1
    assert coordinator.inflight_by_client_order_id["btc-breakout-000004"]["response"] == response


@pytest.mark.asyncio
async def test_execution_coordinator_creator_cancellation_does_not_poison_retry() -> None:
    rest = _BlockingRestClient()
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

    first = asyncio.create_task(
        coordinator.submit_market_open(
            symbol="BTC-USDT-SWAP",
            side="buy",
            size=1.0,
            client_order_id="btc-breakout-000005",
            decision=decision,
            timestamp="2026-04-19T00:00:00.000Z",
        )
    )
    await rest.started.wait()

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    retry = asyncio.create_task(
        coordinator.submit_market_open(
            symbol="BTC-USDT-SWAP",
            side="buy",
            size=1.0,
            client_order_id="btc-breakout-000005",
            decision=decision,
            timestamp="2026-04-19T00:00:00.000Z",
        )
    )
    await asyncio.sleep(0)

    rest.release.set()
    response = await retry

    assert response == [{"ordId": "1", "clOrdId": "btc-breakout-000005", "sCode": "0"}]
    assert len(rest.calls) == 1
    assert coordinator.inflight_by_client_order_id["btc-breakout-000005"]["response"] == response


@pytest.mark.asyncio
async def test_execution_coordinator_rejects_cached_client_order_id_reuse_with_different_order_parameters() -> None:
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
        client_order_id="btc-breakout-000003",
        decision=decision,
        timestamp="2026-04-19T00:00:00.000Z",
    )

    with pytest.raises(ValueError, match=r"client_order_id .* original order parameters"):
        await coordinator.submit_market_open(
            symbol="BTC-USDT-SWAP",
            side="sell",
            size=1.0,
            client_order_id="btc-breakout-000003",
            decision=decision,
            timestamp="2026-04-19T00:00:00.000Z",
        )

    assert len(rest.calls) == 1


@pytest.mark.parametrize("size", ["1", None, object()])
def test_build_market_order_payload_rejects_non_numeric_size(size: object) -> None:
    with pytest.raises(ValueError, match=r"invalid size"):
        build_market_order_payload("BTC-USDT-SWAP", "buy", size, "btc-breakout-000001")


@pytest.mark.parametrize("size", [float("nan"), float("inf"), float("-inf")])
def test_build_market_order_payload_rejects_non_finite_size(size: float) -> None:
    with pytest.raises(ValueError, match=r"invalid size"):
        build_market_order_payload("BTC-USDT-SWAP", "buy", size, "btc-breakout-000001")

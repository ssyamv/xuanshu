from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from xuanshu.contracts.events import (
    AccountSnapshotEvent,
    FaultEvent,
    MarketTradeEvent,
    OrderUpdateEvent,
    OrderbookTopEvent,
    PositionUpdateEvent,
)
from xuanshu.core.enums import TraderEventType


def test_trader_event_contracts_are_stable_and_typed() -> None:
    generated_at = datetime.now(UTC)

    orderbook = OrderbookTopEvent(
        event_type=TraderEventType.ORDERBOOK_TOP,
        symbol="BTC-USDT-SWAP",
        exchange="okx",
        generated_at=generated_at,
        public_sequence="pub-1",
        bid_price=100.0,
        ask_price=100.1,
        bid_size=5.0,
        ask_size=6.0,
    )
    trade = MarketTradeEvent(
        event_type=TraderEventType.MARKET_TRADE,
        symbol="BTC-USDT-SWAP",
        exchange="okx",
        generated_at=generated_at,
        public_sequence="pub-2",
        price=100.2,
        size=1.5,
        side="buy",
    )
    order = OrderUpdateEvent(
        event_type=TraderEventType.ORDER_UPDATE,
        symbol="BTC-USDT-SWAP",
        exchange="okx",
        generated_at=generated_at,
        private_sequence="pri-1",
        order_id="123",
        client_order_id="btc-breakout-000001",
        side="buy",
        price=100.2,
        size=1.0,
        filled_size=0.0,
        status="live",
    )
    position = PositionUpdateEvent(
        event_type=TraderEventType.POSITION_UPDATE,
        symbol="BTC-USDT-SWAP",
        exchange="okx",
        generated_at=generated_at,
        private_sequence="pri-2",
        net_quantity=1.0,
        average_price=100.2,
        mark_price=100.4,
        unrealized_pnl=0.2,
    )
    account = AccountSnapshotEvent(
        event_type=TraderEventType.ACCOUNT_SNAPSHOT,
        exchange="okx",
        generated_at=generated_at,
        private_sequence="pri-3",
        equity=1000.0,
        available_balance=800.0,
        margin_ratio=0.15,
    )
    fault = FaultEvent(
        event_type=TraderEventType.RUNTIME_FAULT,
        exchange="okx",
        generated_at=generated_at,
        severity="warn",
        code="public_ws_disconnected",
        detail="public stream dropped",
    )

    assert orderbook.event_type == TraderEventType.ORDERBOOK_TOP
    assert trade.side == "buy"
    assert order.status == "live"
    assert position.net_quantity == 1.0
    assert account.available_balance == 800.0
    assert fault.code == "public_ws_disconnected"


def test_trader_event_contracts_reject_invalid_sequences_and_prices() -> None:
    generated_at = datetime.now(UTC)

    with pytest.raises(ValidationError):
        OrderbookTopEvent(
            event_type=TraderEventType.ORDERBOOK_TOP,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=generated_at,
            public_sequence="",
            bid_price=100.0,
            ask_price=99.9,
            bid_size=5.0,
            ask_size=6.0,
        )

    with pytest.raises(ValidationError):
        OrderbookTopEvent(
            event_type=TraderEventType.ORDERBOOK_TOP,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=generated_at,
            public_sequence=" ",
            bid_price=100.0,
            ask_price=100.1,
            bid_size=5.0,
            ask_size=6.0,
        )

    with pytest.raises(ValidationError):
        AccountSnapshotEvent(
            event_type=TraderEventType.ACCOUNT_SNAPSHOT,
            exchange="okx",
            generated_at=generated_at,
            private_sequence=" ",
            equity=1000.0,
            available_balance=800.0,
            margin_ratio=0.15,
        )

    with pytest.raises(ValidationError):
        PositionUpdateEvent(
            event_type=TraderEventType.POSITION_UPDATE,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=generated_at,
            private_sequence="pri-2",
            net_quantity=1.0,
            average_price=-1.0,
            mark_price=100.4,
            unrealized_pnl=0.2,
        )

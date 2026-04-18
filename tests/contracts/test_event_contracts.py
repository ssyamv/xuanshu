from datetime import UTC, datetime
from math import inf, nan

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


def test_trader_event_contracts_reject_invalid_sequences_and_whitespace_identifiers() -> None:
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
        OrderbookTopEvent(
            event_type=TraderEventType.ORDERBOOK_TOP,
            symbol="BTC-USDT-SWAP",
            exchange=" ",
            generated_at=generated_at,
            public_sequence="pub-1",
            bid_price=100.0,
            ask_price=100.1,
            bid_size=5.0,
            ask_size=6.0,
        )

    with pytest.raises(ValidationError):
        OrderbookTopEvent(
            event_type=TraderEventType.ORDERBOOK_TOP,
            symbol=" ",
            exchange="okx",
            generated_at=generated_at,
            public_sequence="pub-1",
            bid_price=100.0,
            ask_price=100.1,
            bid_size=5.0,
            ask_size=6.0,
        )

    with pytest.raises(ValidationError):
        OrderUpdateEvent(
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
            status=" ",
        )


def test_trader_event_contracts_reject_invalid_prices() -> None:
    generated_at = datetime.now(UTC)

    with pytest.raises(ValidationError):
        OrderbookTopEvent(
            event_type=TraderEventType.ORDERBOOK_TOP,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=generated_at,
            public_sequence="pub-1",
            bid_price=100.0,
            ask_price=99.9,
            bid_size=5.0,
            ask_size=6.0,
        )


def test_trader_event_contracts_reject_unknown_fields() -> None:
    generated_at = datetime.now(UTC)

    with pytest.raises(ValidationError):
        OrderbookTopEvent(
            event_type=TraderEventType.ORDERBOOK_TOP,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=generated_at,
            public_sequence="pub-1",
            bid_price=100.0,
            ask_price=100.1,
            bid_size=5.0,
            ask_size=6.0,
            unexpected_field="nope",
        )


def test_order_update_event_rejects_filled_size_over_size() -> None:
    generated_at = datetime.now(UTC)

    with pytest.raises(ValidationError):
        OrderUpdateEvent(
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
            filled_size=1.1,
            status="live",
        )


def test_trader_event_contracts_reject_naive_generated_at() -> None:
    with pytest.raises(ValidationError):
        OrderbookTopEvent(
            event_type=TraderEventType.ORDERBOOK_TOP,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(),
            public_sequence="pub-1",
            bid_price=100.0,
            ask_price=100.1,
            bid_size=5.0,
            ask_size=6.0,
        )


@pytest.mark.parametrize(
    ("model", "field", "base_kwargs"),
    [
        (
            OrderbookTopEvent,
            "bid_price",
            dict(
                event_type=TraderEventType.ORDERBOOK_TOP,
                symbol="BTC-USDT-SWAP",
                exchange="okx",
                generated_at=datetime.now(UTC),
                public_sequence="pub-1",
                ask_price=100.1,
                bid_size=5.0,
                ask_size=6.0,
            ),
        ),
        (
            MarketTradeEvent,
            "price",
            dict(
                event_type=TraderEventType.MARKET_TRADE,
                symbol="BTC-USDT-SWAP",
                exchange="okx",
                generated_at=datetime.now(UTC),
                public_sequence="pub-2",
                size=1.5,
                side="buy",
            ),
        ),
        (
            OrderUpdateEvent,
            "filled_size",
            dict(
                event_type=TraderEventType.ORDER_UPDATE,
                symbol="BTC-USDT-SWAP",
                exchange="okx",
                generated_at=datetime.now(UTC),
                private_sequence="pri-1",
                order_id="123",
                client_order_id="btc-breakout-000001",
                side="buy",
                price=100.2,
                size=1.0,
                status="live",
            ),
        ),
        (
            PositionUpdateEvent,
            "unrealized_pnl",
            dict(
                event_type=TraderEventType.POSITION_UPDATE,
                symbol="BTC-USDT-SWAP",
                exchange="okx",
                generated_at=datetime.now(UTC),
                private_sequence="pri-2",
                net_quantity=1.0,
                average_price=100.2,
                mark_price=100.4,
            ),
        ),
        (
            AccountSnapshotEvent,
            "margin_ratio",
            dict(
                event_type=TraderEventType.ACCOUNT_SNAPSHOT,
                exchange="okx",
                generated_at=datetime.now(UTC),
                private_sequence="pri-3",
                equity=1000.0,
                available_balance=800.0,
            ),
        ),
    ],
)
@pytest.mark.parametrize("bad_value", [inf, -inf, nan])
def test_trader_event_contracts_reject_non_finite_numeric_values(
    model: type[object],
    field: str,
    base_kwargs: dict[str, object],
    bad_value: float,
) -> None:
    with pytest.raises(ValidationError):
        model(**base_kwargs, **{field: bad_value})

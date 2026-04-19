import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

from xuanshu.contracts.events import (
    AccountSnapshotEvent,
    FaultEvent,
    MarketTradeEvent,
    OrderUpdateEvent,
    OrderbookTopEvent,
    PositionUpdateEvent,
)
from xuanshu.core.enums import TraderEventType
from xuanshu.infra.okx.private_ws import OkxPrivateStream
from xuanshu.infra.okx.public_ws import OkxPublicStream
from xuanshu.infra.okx.rest import OkxBusinessError, OkxRestClient


class _FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    def __aiter__(self):
        async def _generator():
            for message in self.messages:
                yield message

        return _generator()


class _GuardedPrivateWebSocket(_FakeWebSocket):
    def __init__(self, messages: list[str]) -> None:
        super().__init__(messages)
        self.login_ack_consumed = False

    async def send(self, payload: str) -> None:
        message = json.loads(payload)
        if message.get("op") == "subscribe" and not self.login_ack_consumed:
            raise AssertionError("subscribe sent before login ack was consumed")
        await super().send(payload)

    def __aiter__(self):
        async def _generator():
            for message in self.messages:
                payload = json.loads(message)
                if payload.get("event") == "login":
                    self.login_ack_consumed = True
                yield message

        return _generator()


class _FakeConnect:
    def __init__(self, websocket: _FakeWebSocket) -> None:
        self.websocket = websocket
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, url: str, **kwargs: object):
        self.calls.append((url, dict(kwargs)))
        websocket = self.websocket

        class _ContextManager:
            async def __aenter__(self_nonlocal):
                return websocket

            async def __aexit__(self_nonlocal, exc_type, exc, tb) -> None:
                return None

        return _ContextManager()


def test_okx_public_stream_builds_bbo_subscription_and_decodes_batched_events() -> None:
    stream = OkxPublicStream(url="wss://ws.okx.com:8443/ws/v5/public")

    payload = stream.build_subscribe_payload(("BTC-USDT-SWAP",))
    ticker_message = {
        "arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"},
        "data": [
            {
                "ts": "1713484800000",
                "bidPx": "100.0",
                "askPx": "100.1",
                "bidSz": "5",
                "askSz": "6",
            },
            {
                "ts": "1713484801000",
                "bidPx": "100.1",
                "askPx": "100.2",
                "bidSz": "7",
                "askSz": "8",
            },
        ],
    }
    trades_message = {
        "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
        "data": [
            {
                "ts": "1713484802000",
                "px": "100.3",
                "sz": "1.2",
                "side": "buy",
            },
            {
                "ts": "1713484803000",
                "px": "100.4",
                "sz": "0.4",
                "side": "sell",
            },
        ],
    }

    ticker_events = stream.decode_message(ticker_message, sequence="pub-1")
    trade_events = stream.decode_message(trades_message, sequence="pub-2")

    assert payload == {
        "op": "subscribe",
        "args": [
            {"channel": "tickers", "instId": "BTC-USDT-SWAP"},
            {"channel": "trades", "instId": "BTC-USDT-SWAP"},
        ],
    }
    assert [type(event) for event in ticker_events] == [OrderbookTopEvent, OrderbookTopEvent]
    assert ticker_events[0].bid_price == 100.0
    assert ticker_events[1].bid_price == 100.1
    assert [type(event) for event in trade_events] == [MarketTradeEvent, MarketTradeEvent]
    assert trade_events[0].side == "buy"
    assert trade_events[1].side == "sell"


def test_okx_public_stream_ignores_empty_data_batches() -> None:
    stream = OkxPublicStream(url="wss://ws.okx.com:8443/ws/v5/public")

    events = stream.decode_message({"arg": {"channel": "tickers"}, "data": []}, sequence="pub-1")

    assert events == ()


def test_okx_public_stream_normalizes_malformed_and_unknown_envelopes_into_faults() -> None:
    stream = OkxPublicStream(url="wss://ws.okx.com:8443/ws/v5/public")

    malformed_arg_events = stream.decode_message(
        {"arg": "tickers", "data": [{"ts": "1713484800000"}]},
        sequence="pub-1",
    )
    malformed_data_events = stream.decode_message(
        {
            "arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"},
            "data": {"ts": "1713484800000"},
        },
        sequence="pub-2",
    )
    unknown_channel_events = stream.decode_message(
        {
            "arg": {"channel": "books", "instId": "BTC-USDT-SWAP"},
            "data": [{"ts": "1713484800000"}],
        },
        sequence="pub-3",
    )

    assert len(malformed_arg_events) == 1
    assert isinstance(malformed_arg_events[0], FaultEvent)
    assert malformed_arg_events[0].code == "public_ws_malformed_envelope"
    assert len(malformed_data_events) == 1
    assert isinstance(malformed_data_events[0], FaultEvent)
    assert malformed_data_events[0].code == "public_ws_malformed_envelope"
    assert len(unknown_channel_events) == 1
    assert isinstance(unknown_channel_events[0], FaultEvent)
    assert unknown_channel_events[0].code == "public_ws_unknown_channel"


def test_okx_public_stream_normalizes_pydantic_validation_failures_into_faults() -> None:
    stream = OkxPublicStream(url="wss://ws.okx.com:8443/ws/v5/public")

    events = stream.decode_message(
        {
            "arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"},
            "data": [
                {
                    "ts": "1713484800000",
                    "bidPx": "100.2",
                    "askPx": "100.1",
                    "bidSz": "5",
                    "askSz": "6",
                }
            ],
        },
        sequence="pub-1",
    )

    assert len(events) == 1
    assert isinstance(events[0], FaultEvent)
    assert events[0].code == "public_ws_error"
    assert "ask_price" in events[0].detail


def test_okx_private_stream_builds_login_and_decodes_order_position_and_account_batches() -> None:
    stream = OkxPrivateStream(url="wss://ws.okx.com:8443/ws/v5/private")

    login = stream.build_login_payload(
        api_key="test-key",
        api_secret="test-secret",
        passphrase="test-passphrase",
        epoch_seconds=1713484800,
    )
    order_message = {
        "arg": {"channel": "orders", "instType": "SWAP"},
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "ordId": "1",
                "clOrdId": "btc-breakout-000001",
                "side": "buy",
                "px": "100.2",
                "sz": "1",
                "accFillSz": "0",
                "state": "live",
                "uTime": "1713484800000",
            },
            {
                "instId": "BTC-USDT-SWAP",
                "ordId": "2",
                "clOrdId": "btc-breakout-000002",
                "side": "sell",
                "px": "100.4",
                "sz": "2",
                "accFillSz": "1",
                "state": "partially_filled",
                "uTime": "1713484801000",
            },
        ],
    }
    position_message = {
        "arg": {"channel": "positions", "instType": "SWAP"},
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "avgPx": "100.2",
                "markPx": "100.4",
                "pos": "1",
                "upl": "0.2",
                "uTime": "1713484805000",
            }
        ],
    }
    account_message = {
        "arg": {"channel": "account"},
        "data": [
            {
                "totalEq": "1000",
                "availEq": "800",
                "mgnRatio": "0.15",
                "uTime": "1713484810000",
            }
        ],
    }

    order_events = stream.decode_message(order_message, sequence="pri-1")
    position_events = stream.decode_message(position_message, sequence="pri-2")
    account_events = stream.decode_message(account_message, sequence="pri-3")

    expected_sign = hmac.new(
        b"test-secret",
        b"1713484800GET/users/self/verify",
        hashlib.sha256,
    ).digest()

    assert login["op"] == "login"
    assert login["args"][0]["apiKey"] == "test-key"
    assert login["args"][0]["passphrase"] == "test-passphrase"
    assert login["args"][0]["sign"] == base64.b64encode(expected_sign).decode()
    assert [type(event) for event in order_events] == [OrderUpdateEvent, OrderUpdateEvent]
    assert order_events[0].order_id == "1"
    assert order_events[1].status == "partially_filled"
    assert len(position_events) == 1
    assert isinstance(position_events[0], PositionUpdateEvent)
    assert position_events[0].net_quantity == 1.0
    assert len(account_events) == 1
    assert isinstance(account_events[0], AccountSnapshotEvent)
    assert account_events[0].available_balance == 800.0


def test_okx_private_stream_normalizes_fault_payloads() -> None:
    stream = OkxPrivateStream(url="wss://ws.okx.com:8443/ws/v5/private")

    login_faults = stream.decode_message(
        {
            "event": "login",
            "code": "60009",
            "msg": "Login failed.",
            "connId": "a4d3ae55",
        },
        sequence="pri-1",
    )
    stream_faults = stream.decode_message(
        {
            "event": "error",
            "code": "60012",
            "msg": "Invalid request",
        },
        sequence="pri-2",
    )

    assert len(login_faults) == 1
    assert isinstance(login_faults[0], FaultEvent)
    assert login_faults[0].event_type == TraderEventType.RUNTIME_FAULT
    assert login_faults[0].code == "60009"
    assert "Login failed." in login_faults[0].detail
    assert len(stream_faults) == 1
    assert isinstance(stream_faults[0], FaultEvent)
    assert stream_faults[0].code == "60012"


def test_okx_private_stream_ignores_control_messages_without_arg() -> None:
    stream = OkxPrivateStream(url="wss://ws.okx.com:8443/ws/v5/private")

    events = stream.decode_message(
        {"connId": "abc123", "code": "0", "msg": ""},
        sequence="pri-1",
    )

    assert events == ()


def test_okx_private_stream_handles_optional_blank_fields_without_crashing() -> None:
    stream = OkxPrivateStream(url="wss://ws.okx.com:8443/ws/v5/private")

    order_events = stream.decode_message(
        {
            "arg": {"channel": "orders", "instType": "SWAP"},
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "1",
                    "clOrdId": "",
                    "side": "buy",
                    "px": "",
                    "sz": "1",
                    "accFillSz": "",
                    "state": "live",
                    "uTime": "1713484800000",
                }
            ],
        },
        sequence="pri-1",
    )
    position_events = stream.decode_message(
        {
            "arg": {"channel": "positions", "instType": "SWAP"},
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "avgPx": "",
                    "markPx": "",
                    "pos": "0",
                    "upl": "0",
                    "uTime": "1713484805000",
                }
            ],
        },
        sequence="pri-2",
    )

    assert len(order_events) == 1
    assert isinstance(order_events[0], OrderUpdateEvent)
    assert order_events[0].client_order_id == "1"
    assert order_events[0].price == 0.0
    assert order_events[0].filled_size == 0.0
    assert len(position_events) == 1
    assert isinstance(position_events[0], FaultEvent)
    assert position_events[0].code == "positions_decode_error"


def test_okx_private_stream_normalizes_blank_account_and_position_numerics_into_faults() -> None:
    stream = OkxPrivateStream(url="wss://ws.okx.com:8443/ws/v5/private")

    position_events = stream.decode_message(
        {
            "arg": {"channel": "positions", "instType": "SWAP"},
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "avgPx": "",
                    "markPx": "100.4",
                    "pos": "1",
                    "upl": "0.2",
                    "uTime": "1713484805000",
                }
            ],
        },
        sequence="pri-2",
    )
    account_events = stream.decode_message(
        {
            "arg": {"channel": "account"},
            "data": [
                {
                    "totalEq": "1000",
                    "availEq": "",
                    "mgnRatio": "0.15",
                    "uTime": "1713484810000",
                }
            ],
        },
        sequence="pri-3",
    )

    assert len(position_events) == 1
    assert isinstance(position_events[0], FaultEvent)
    assert position_events[0].code == "positions_decode_error"
    assert "avgPx" in position_events[0].detail
    assert len(account_events) == 1
    assert isinstance(account_events[0], AccountSnapshotEvent)
    assert account_events[0].available_balance == 0.0


def test_okx_private_stream_accepts_blank_available_equity_in_account_updates() -> None:
    stream = OkxPrivateStream(url="wss://ws.okx.com:8443/ws/v5/private")

    account_events = stream.decode_message(
        {
            "arg": {"channel": "account"},
            "data": [
                {
                    "totalEq": "1000",
                    "availEq": "",
                    "mgnRatio": "0.15",
                    "uTime": "1713484810000",
                }
            ],
        },
        sequence="pri-3",
    )

    assert len(account_events) == 1
    assert isinstance(account_events[0], AccountSnapshotEvent)
    assert account_events[0].equity == 1000.0
    assert account_events[0].available_balance == 0.0


def test_okx_private_stream_normalizes_decode_failures_into_faults() -> None:
    stream = OkxPrivateStream(url="wss://ws.okx.com:8443/ws/v5/private")

    events = stream.decode_message(
        {
            "arg": {"channel": "orders", "instType": "SWAP"},
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "1",
                    "clOrdId": "btc-breakout-000001",
                    "side": "buy",
                    "px": "100.2",
                    "sz": "1",
                    "accFillSz": "2",
                    "state": "live",
                    "uTime": "1713484800000",
                }
            ],
        },
        sequence="pri-1",
    )

    assert len(events) == 1
    assert isinstance(events[0], FaultEvent)
    assert events[0].code == "orders_decode_error"
    assert "filled_size" in events[0].detail


def test_okx_private_stream_normalizes_malformed_and_unknown_envelopes_into_faults() -> None:
    stream = OkxPrivateStream(url="wss://ws.okx.com:8443/ws/v5/private")

    malformed_arg_events = stream.decode_message(
        {"arg": "orders", "data": [{"ordId": "1"}]},
        sequence="pri-1",
    )
    malformed_data_events = stream.decode_message(
        {
            "arg": {"channel": "orders", "instType": "SWAP"},
            "data": {"ordId": "1"},
        },
        sequence="pri-2",
    )
    unknown_channel_events = stream.decode_message(
        {
            "arg": {"channel": "algo-advance", "instType": "SWAP"},
            "data": [{"ordId": "1"}],
        },
        sequence="pri-3",
    )

    assert len(malformed_arg_events) == 1
    assert isinstance(malformed_arg_events[0], FaultEvent)
    assert malformed_arg_events[0].code == "private_ws_malformed_envelope"
    assert len(malformed_data_events) == 1
    assert isinstance(malformed_data_events[0], FaultEvent)
    assert malformed_data_events[0].code == "private_ws_malformed_envelope"
    assert len(unknown_channel_events) == 1
    assert isinstance(unknown_channel_events[0], FaultEvent)
    assert unknown_channel_events[0].code == "private_ws_unknown_channel"


@pytest.mark.asyncio
async def test_okx_public_stream_iter_events_subscribes_and_decodes_messages() -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps(
                {
                    "arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"},
                    "data": [
                        {
                            "ts": "1713484800000",
                            "bidPx": "100.0",
                            "askPx": "100.1",
                            "bidSz": "5",
                            "askSz": "6",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
                    "data": [{"ts": "1713484802000", "px": "100.3", "sz": "1.2", "side": "buy"}],
                }
            ),
        ]
    )
    connect = _FakeConnect(websocket)
    stream = OkxPublicStream(url="wss://ws.okx.com:8443/ws/v5/public", connect_factory=connect)

    events = [event async for event in stream.iter_events(symbols=("BTC-USDT-SWAP",))]

    assert [type(event) for event in events] == [OrderbookTopEvent, MarketTradeEvent]
    assert events[0].public_sequence == "pub-1"
    assert events[1].public_sequence == "pub-2"
    assert json.loads(websocket.sent[0]) == stream.build_subscribe_payload(("BTC-USDT-SWAP",))
    assert connect.calls[0][0] == "wss://ws.okx.com:8443/ws/v5/public"


@pytest.mark.asyncio
async def test_okx_private_stream_iter_events_logs_in_subscribes_and_decodes_messages() -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps({"event": "login", "code": "0", "msg": "", "connId": "abc"}),
            json.dumps(
                {
                    "arg": {"channel": "orders", "instType": "SWAP"},
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "ordId": "1",
                            "clOrdId": "btc-breakout-000001",
                            "side": "buy",
                            "px": "100.2",
                            "sz": "1",
                            "accFillSz": "0",
                            "state": "live",
                            "uTime": "1713484800000",
                        }
                    ],
                }
            ),
        ]
    )
    connect = _FakeConnect(websocket)
    stream = OkxPrivateStream(
        url="wss://ws.okx.com:8443/ws/v5/private",
        connect_factory=connect,
        epoch_seconds_factory=lambda: 1713484800,
    )

    events = [
        event
        async for event in stream.iter_events(
            symbols=("BTC-USDT-SWAP",),
            api_key="test-key",
            api_secret="test-secret",
            passphrase="test-passphrase",
        )
    ]

    assert [type(event) for event in events] == [OrderUpdateEvent]
    assert events[0].private_sequence == "pri-2"
    login_payload = json.loads(websocket.sent[0])
    subscribe_payload = json.loads(websocket.sent[1])
    assert login_payload == stream.build_login_payload(
        api_key="test-key",
        api_secret="test-secret",
        passphrase="test-passphrase",
        epoch_seconds=1713484800,
    )
    assert subscribe_payload == stream.build_subscribe_payload(("BTC-USDT-SWAP",))


@pytest.mark.asyncio
async def test_okx_private_stream_waits_for_login_ack_before_subscribing() -> None:
    websocket = _GuardedPrivateWebSocket(
        [
            json.dumps({"event": "login", "code": "0", "msg": "", "connId": "abc"}),
        ]
    )
    connect = _FakeConnect(websocket)
    stream = OkxPrivateStream(
        url="wss://ws.okx.com:8443/ws/v5/private",
        connect_factory=connect,
        epoch_seconds_factory=lambda: 1713484800,
    )

    events = [
        event
        async for event in stream.iter_events(
            symbols=("BTC-USDT-SWAP",),
            api_key="test-key",
            api_secret="test-secret",
            passphrase="test-passphrase",
        )
    ]

    assert events == []
    assert len(websocket.sent) == 2
    assert json.loads(websocket.sent[0])["op"] == "login"
    assert json.loads(websocket.sent[1])["op"] == "subscribe"


@pytest.mark.asyncio
async def test_okx_private_stream_ignores_prelogin_errors_and_subscribes_only_after_success() -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps({"event": "error", "code": "60011", "msg": "Please log in", "connId": "abc"}),
            json.dumps({"event": "login", "code": "0", "msg": "", "connId": "abc"}),
            json.dumps(
                {
                    "arg": {"channel": "account"},
                    "data": [
                        {
                            "totalEq": "1000",
                            "availEq": "800",
                            "mgnRatio": "0.15",
                            "uTime": "1713484810000",
                        }
                    ],
                }
            ),
        ]
    )
    connect = _FakeConnect(websocket)
    stream = OkxPrivateStream(
        url="wss://ws.okx.com:8443/ws/v5/private",
        connect_factory=connect,
        epoch_seconds_factory=lambda: 1713484800,
    )

    events = [
        event
        async for event in stream.iter_events(
            symbols=("BTC-USDT-SWAP",),
            api_key="test-key",
            api_secret="test-secret",
            passphrase="test-passphrase",
        )
    ]

    assert [type(event) for event in events] == [FaultEvent, AccountSnapshotEvent]
    assert events[0].code == "60011"
    assert events[1].private_sequence == "pri-3"
    assert len(websocket.sent) == 2


def test_okx_rest_client_builds_signed_headers_and_place_order_payload() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )

    headers = client.build_signed_headers(
        method="POST",
        path="/api/v5/trade/order",
        body='{"instId":"BTC-USDT-SWAP"}',
        timestamp="2026-04-19T00:00:00.000Z",
    )
    payload = client.build_place_order_payload(
        symbol="BTC-USDT-SWAP",
        side="buy",
        order_type="market",
        size="1",
        client_order_id="btc-breakout-000001",
    )

    assert headers["OK-ACCESS-KEY"] == "api-key"
    assert headers["OK-ACCESS-PASSPHRASE"] == "api-passphrase"
    assert "OK-ACCESS-SIGN" in headers
    assert payload == {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "cross",
        "side": "buy",
        "ordType": "market",
        "sz": "1",
        "clOrdId": "btc-breakout-000001",
    }

    asyncio.run(client.aclose())


def test_okx_rest_client_rejects_invalid_order_type_price_combinations() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )

    try:
        client.build_place_order_payload(
            symbol="BTC-USDT-SWAP",
            side="buy",
            order_type="limit",
            size="1",
            client_order_id="btc-breakout-000001",
        )
    except ValueError as exc:
        assert "price" in str(exc)
    else:
        raise AssertionError("expected ValueError when limit order omits price")

    try:
        client.build_place_order_payload(
            symbol="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            size="1",
            client_order_id="btc-breakout-000002",
            price="100.0",
        )
    except ValueError as exc:
        assert "price" in str(exc)
    else:
        raise AssertionError("expected ValueError when market order includes price")

    asyncio.run(client.aclose())


def test_okx_rest_client_rejects_unsupported_order_type() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )

    try:
        client.build_place_order_payload(
            symbol="BTC-USDT-SWAP",
            side="buy",
            order_type="post_only",
            size="1",
            client_order_id="btc-breakout-000003",
            price="100.0",
        )
    except ValueError as exc:
        assert "order_type" in str(exc)
    else:
        raise AssertionError("expected ValueError for unsupported order_type")

    asyncio.run(client.aclose())


def test_okx_rest_client_rejects_invalid_side() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )

    try:
        client.build_place_order_payload(
            symbol="BTC-USDT-SWAP",
            side="hold",
            order_type="market",
            size="1",
            client_order_id="btc-breakout-000004",
        )
    except ValueError as exc:
        assert "side" in str(exc)
    else:
        raise AssertionError("expected ValueError for invalid side")

    asyncio.run(client.aclose())


def test_okx_rest_client_place_order_posts_signed_body() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )
    payload = {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "cross",
        "side": "buy",
        "ordType": "market",
        "sz": "1",
        "clOrdId": "btc-breakout-000001",
    }
    timestamp = "2026-04-19T00:00:00.000Z"
    expected_body = json.dumps(payload, separators=(",", ":"))
    expected_signature = base64.b64encode(
        hmac.new(
            b"api-secret",
            f"{timestamp}POST/api/v5/trade/order{expected_body}".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()

    captured: dict[str, object] = {}

    class DummyResponse:
        def raise_for_status(self) -> None:
            captured["raise_for_status_called"] = True

        def json(self) -> dict[str, object]:
            return {"code": "0", "data": [{"ordId": "12345", "sCode": "0"}]}

    async def fake_post(path: str, *, content: str, headers: dict[str, str]) -> DummyResponse:
        captured["path"] = path
        captured["content"] = content
        captured["headers"] = headers
        return DummyResponse()

    client.client.post = fake_post  # type: ignore[method-assign]

    result = asyncio.run(client.place_order(payload, timestamp))

    assert result == [{"ordId": "12345", "sCode": "0"}]
    assert captured["path"] == "/api/v5/trade/order"
    assert captured["content"] == expected_body
    assert captured["headers"] == {
        "OK-ACCESS-KEY": "api-key",
        "OK-ACCESS-PASSPHRASE": "api-passphrase",
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-SIGN": expected_signature,
        "Content-Type": "application/json",
    }
    assert captured["raise_for_status_called"] is True

    asyncio.run(client.aclose())


def test_okx_rest_client_treats_top_level_non_zero_code_as_failure() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )
    timestamp = "2026-04-19T00:00:00.000Z"

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "code": "51000",
                "msg": "Parameter error",
                "data": [],
            }

    async def fake_get(path: str, *, headers: dict[str, str]) -> DummyResponse:
        return DummyResponse()

    client.client.get = fake_get  # type: ignore[method-assign]

    with pytest.raises(OkxBusinessError, match="51000"):
        asyncio.run(client.fetch_account_summary(timestamp))

    asyncio.run(client.aclose())


def test_okx_rest_client_place_order_treats_non_zero_scode_as_failure() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )
    payload = {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "cross",
        "side": "buy",
        "ordType": "market",
        "sz": "1",
        "clOrdId": "btc-breakout-000001",
    }
    timestamp = "2026-04-19T00:00:00.000Z"

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "ordId": "",
                        "clOrdId": "btc-breakout-000001",
                        "sCode": "51008",
                        "sMsg": "Order failed",
                    }
                ],
            }

    async def fake_post(path: str, *, content: str, headers: dict[str, str]) -> DummyResponse:
        return DummyResponse()

    client.client.post = fake_post  # type: ignore[method-assign]

    with pytest.raises(OkxBusinessError, match="51008"):
        asyncio.run(client.place_order(payload, timestamp))

    asyncio.run(client.aclose())


def test_okx_rest_client_place_order_rejects_malformed_payload_before_post() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )
    timestamp = "2026-04-19T00:00:00.000Z"
    post_called = False

    async def fake_post(path: str, *, content: str, headers: dict[str, str]) -> None:
        nonlocal post_called
        post_called = True

    client.client.post = fake_post  # type: ignore[method-assign]

    try:
        asyncio.run(
            client.place_order(
                {
                    "instId": "BTC-USDT-SWAP",
                    "tdMode": "cross",
                    "side": "buy",
                    "ordType": "stop",
                    "sz": "1",
                    "clOrdId": "btc-breakout-000005",
                },
                timestamp,
            )
        )
    except ValueError as exc:
        assert "order_type" in str(exc)
    else:
        raise AssertionError("expected ValueError for malformed direct place_order payload")

    assert post_called is False

    asyncio.run(client.aclose())


def test_okx_rest_client_place_order_rejects_blank_required_payload_values() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )
    timestamp = "2026-04-19T00:00:00.000Z"
    post_called = False

    async def fake_post(path: str, *, content: str, headers: dict[str, str]) -> None:
        nonlocal post_called
        post_called = True

    client.client.post = fake_post  # type: ignore[method-assign]

    invalid_payloads = (
        {
            "instId": "",
            "tdMode": "cross",
            "side": "buy",
            "ordType": "market",
            "sz": "1",
            "clOrdId": "btc-breakout-000006",
        },
        {
            "instId": "BTC-USDT-SWAP",
            "tdMode": "cross",
            "side": "buy",
            "ordType": "market",
            "sz": "",
            "clOrdId": "btc-breakout-000007",
        },
        {
            "instId": "BTC-USDT-SWAP",
            "tdMode": "cross",
            "side": "buy",
            "ordType": "market",
            "sz": "1",
            "clOrdId": "",
        },
    )

    for payload in invalid_payloads:
        try:
            asyncio.run(client.place_order(payload, timestamp))
        except ValueError as exc:
            assert "blank" in str(exc)
        else:
            raise AssertionError("expected ValueError for blank required payload value")

    assert post_called is False

    asyncio.run(client.aclose())


def test_okx_rest_client_rejects_blank_limit_price() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )
    timestamp = "2026-04-19T00:00:00.000Z"
    post_called = False

    async def fake_post(path: str, *, content: str, headers: dict[str, str]) -> None:
        nonlocal post_called
        post_called = True

    client.client.post = fake_post  # type: ignore[method-assign]

    try:
        asyncio.run(
            client.place_order(
                {
                    "instId": "BTC-USDT-SWAP",
                    "tdMode": "cross",
                    "side": "buy",
                    "ordType": "limit",
                    "sz": "1",
                    "clOrdId": "btc-breakout-000008",
                    "px": "",
                },
                timestamp,
            )
        )
    except ValueError as exc:
        assert "blank" in str(exc)
        assert "price" in str(exc)
    else:
        raise AssertionError("expected ValueError for blank limit price")

    assert post_called is False

    asyncio.run(client.aclose())


def test_okx_rest_client_place_order_rejects_unexpected_extra_payload_keys() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )
    timestamp = "2026-04-19T00:00:00.000Z"
    post_called = False

    async def fake_post(path: str, *, content: str, headers: dict[str, str]) -> None:
        nonlocal post_called
        post_called = True

    client.client.post = fake_post  # type: ignore[method-assign]

    try:
        asyncio.run(
            client.place_order(
                {
                    "instId": "BTC-USDT-SWAP",
                    "tdMode": "cross",
                    "side": "buy",
                    "ordType": "market",
                    "sz": "1",
                    "clOrdId": "btc-breakout-000009",
                    "foo": "bar",
                },
                timestamp,
            )
        )
    except ValueError as exc:
        assert "unexpected" in str(exc)
    else:
        raise AssertionError("expected ValueError for unexpected direct place_order payload key")

    assert post_called is False

    asyncio.run(client.aclose())


def test_okx_rest_client_fetches_open_orders_positions_and_account_summary() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )
    timestamp = "2026-04-19T00:00:00.000Z"
    captured: list[tuple[str, str, dict[str, str]]] = []

    class DummyResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    async def fake_get(path: str, *, headers: dict[str, str]) -> DummyResponse:
        captured.append((path, headers["OK-ACCESS-TIMESTAMP"], headers))
        return DummyResponse({"code": "0", "data": [{"path": path}]})

    client.client.get = fake_get  # type: ignore[method-assign]

    open_orders = asyncio.run(client.fetch_open_orders("BTC-USDT-SWAP", timestamp))
    positions = asyncio.run(client.fetch_positions("BTC-USDT-SWAP", timestamp))
    account = asyncio.run(client.fetch_account_summary(timestamp))

    assert open_orders == [{"path": "/api/v5/trade/orders-pending?instId=BTC-USDT-SWAP"}]
    assert positions == [{"path": "/api/v5/account/positions?instId=BTC-USDT-SWAP"}]
    assert account == [{"path": "/api/v5/account/balance"}]
    assert captured[0][0] == "/api/v5/trade/orders-pending?instId=BTC-USDT-SWAP"
    assert captured[1][0] == "/api/v5/account/positions?instId=BTC-USDT-SWAP"
    assert captured[2][0] == "/api/v5/account/balance"
    assert all(item[1] == timestamp for item in captured)
    expected_open_orders_signature = base64.b64encode(
        hmac.new(
            b"api-secret",
            f"{timestamp}GET/api/v5/trade/orders-pending?instId=BTC-USDT-SWAP".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    expected_positions_signature = base64.b64encode(
        hmac.new(
            b"api-secret",
            f"{timestamp}GET/api/v5/account/positions?instId=BTC-USDT-SWAP".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    assert captured[0][2]["OK-ACCESS-SIGN"] == expected_open_orders_signature
    assert captured[1][2]["OK-ACCESS-SIGN"] == expected_positions_signature

    asyncio.run(client.aclose())


def test_okx_rest_client_rejects_blank_symbols_for_signed_getters() -> None:
    client = OkxRestClient(
        base_url="https://www.okx.com",
        api_key="api-key",
        api_secret="api-secret",
        passphrase="api-passphrase",
    )
    timestamp = "2026-04-19T00:00:00.000Z"
    get_called = False

    async def fake_get(path: str, *, headers: dict[str, str]) -> None:
        nonlocal get_called
        get_called = True

    client.client.get = fake_get  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="blank instId is not allowed"):
        asyncio.run(client.fetch_open_orders("   ", timestamp))

    with pytest.raises(ValueError, match="blank instId is not allowed"):
        asyncio.run(client.fetch_positions("\t", timestamp))

    assert get_called is False

    asyncio.run(client.aclose())

import hashlib
import hmac

from xuanshu.contracts.events import OrderUpdateEvent, OrderbookTopEvent, PositionUpdateEvent
from xuanshu.core.enums import TraderEventType
from xuanshu.infra.okx.private_ws import OkxPrivateStream
from xuanshu.infra.okx.public_ws import OkxPublicStream
from xuanshu.infra.okx.rest import OkxRestClient


def test_okx_public_stream_builds_bbo_subscription_and_decodes_events() -> None:
    stream = OkxPublicStream(url="wss://ws.okx.com:8443/ws/v5/public")

    payload = stream.build_subscribe_payload(("BTC-USDT-SWAP",))
    message = {
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

    event = stream.decode_message(message, sequence="pub-1")

    assert payload == {
        "op": "subscribe",
        "args": [{"channel": "tickers", "instId": "BTC-USDT-SWAP"}],
    }
    assert isinstance(event, OrderbookTopEvent)
    assert event.event_type == TraderEventType.ORDERBOOK_TOP
    assert event.bid_price == 100.0


def test_okx_private_stream_builds_login_and_decodes_order_and_position_events() -> None:
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
            }
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

    order_event = stream.decode_message(order_message, sequence="pri-1")
    position_event = stream.decode_message(position_message, sequence="pri-2")

    expected_sign = hmac.new(
        b"test-secret",
        b"1713484800GET/users/self/verify",
        hashlib.sha256,
    ).digest()

    assert login["op"] == "login"
    assert login["args"][0]["apiKey"] == "test-key"
    assert login["args"][0]["passphrase"] == "test-passphrase"
    assert login["args"][0]["sign"] == __import__("base64").b64encode(expected_sign).decode()
    assert isinstance(order_event, OrderUpdateEvent)
    assert order_event.order_id == "1"
    assert isinstance(position_event, PositionUpdateEvent)
    assert position_event.net_quantity == 1.0


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

    __import__("asyncio").run(client.aclose())

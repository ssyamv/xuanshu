from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from xuanshu.contracts.events import MarketTradeEvent, OrderbookTopEvent
from xuanshu.core.enums import TraderEventType


@dataclass(frozen=True, slots=True)
class OkxPublicStream:
    url: str

    def build_subscribe_payload(self, symbols: tuple[str, ...]) -> dict[str, object]:
        return {
            "op": "subscribe",
            "args": [
                {"channel": channel, "instId": symbol}
                for symbol in symbols
                for channel in ("tickers", "trades")
            ],
        }

    def decode_message(
        self, payload: dict[str, Any], sequence: str
    ) -> OrderbookTopEvent | MarketTradeEvent | None:
        channel = payload.get("arg", {}).get("channel")
        data = payload.get("data") or []
        if not data:
            return None
        item = data[0]
        generated_at = datetime.fromtimestamp(int(item["ts"]) / 1000, tz=UTC)
        if channel == "tickers":
            return OrderbookTopEvent(
                event_type=TraderEventType.ORDERBOOK_TOP,
                symbol=payload["arg"]["instId"],
                exchange="okx",
                generated_at=generated_at,
                public_sequence=sequence,
                bid_price=float(item["bidPx"]),
                ask_price=float(item["askPx"]),
                bid_size=float(item["bidSz"]),
                ask_size=float(item["askSz"]),
            )
        if channel == "trades":
            return MarketTradeEvent(
                event_type=TraderEventType.MARKET_TRADE,
                symbol=payload["arg"]["instId"],
                exchange="okx",
                generated_at=generated_at,
                public_sequence=sequence,
                price=float(item["px"]),
                size=float(item["sz"]),
                side=item["side"],
            )
        return None

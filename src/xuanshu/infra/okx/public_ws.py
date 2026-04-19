from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from xuanshu.contracts.events import FaultEvent, MarketTradeEvent, OrderbookTopEvent
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
    ) -> tuple[OrderbookTopEvent | MarketTradeEvent | FaultEvent, ...]:
        event = payload.get("event")
        if event == "error":
            return (self._build_fault(payload),)

        channel = payload.get("arg", {}).get("channel")
        data = payload.get("data") or []
        if not data:
            return ()

        events: list[OrderbookTopEvent | MarketTradeEvent | FaultEvent] = []
        for item in data:
            try:
                generated_at = datetime.fromtimestamp(int(item["ts"]) / 1000, tz=UTC)
                if channel == "tickers":
                    events.append(
                        OrderbookTopEvent(
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
                    )
                    continue
                if channel == "trades":
                    events.append(
                        MarketTradeEvent(
                            event_type=TraderEventType.MARKET_TRADE,
                            symbol=payload["arg"]["instId"],
                            exchange="okx",
                            generated_at=generated_at,
                            public_sequence=sequence,
                            price=float(item["px"]),
                            size=float(item["sz"]),
                            side=item["side"],
                        )
                    )
            except (KeyError, TypeError, ValueError) as exc:
                events.append(self._build_fault(payload, detail=str(exc)))
        return tuple(events)

    def _build_fault(
        self,
        payload: dict[str, Any],
        *,
        detail: str | None = None,
    ) -> FaultEvent:
        return FaultEvent(
            event_type=TraderEventType.RUNTIME_FAULT,
            exchange="okx",
            generated_at=datetime.now(UTC),
            severity="warn",
            code=str(payload.get("code") or "public_ws_error"),
            detail=(detail or str(payload.get("msg") or "public websocket fault")).strip(),
        )

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

        envelope = self._normalize_envelope(payload)
        if isinstance(envelope, FaultEvent):
            return (envelope,)

        channel, symbol, data = envelope
        if not data:
            return ()
        if channel not in {"tickers", "trades"}:
            return (
                self._build_fault(
                    payload,
                    code="public_ws_unknown_channel",
                    detail=f"unknown public channel: {channel}",
                ),
            )

        events: list[OrderbookTopEvent | MarketTradeEvent | FaultEvent] = []
        for item in data:
            try:
                generated_at = datetime.fromtimestamp(int(item["ts"]) / 1000, tz=UTC)
                if channel == "tickers":
                    events.append(
                        OrderbookTopEvent(
                            event_type=TraderEventType.ORDERBOOK_TOP,
                            symbol=symbol,
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
                            symbol=symbol,
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

    def _normalize_envelope(
        self, payload: dict[str, Any]
    ) -> tuple[str, str, list[dict[str, Any]]] | FaultEvent:
        arg = payload.get("arg")
        if not isinstance(arg, dict):
            return self._build_fault(
                payload,
                code="public_ws_malformed_envelope",
                detail="public websocket envelope arg must be an object",
            )

        channel = arg.get("channel")
        if not isinstance(channel, str) or not channel.strip():
            return self._build_fault(
                payload,
                code="public_ws_malformed_envelope",
                detail="public websocket envelope channel must be a non-empty string",
            )

        data = payload.get("data")
        if data is None:
            return (channel.strip(), "", [])
        if not isinstance(data, list):
            return self._build_fault(
                payload,
                code="public_ws_malformed_envelope",
                detail="public websocket envelope data must be a list",
            )
        if not data:
            return (channel.strip(), "", [])
        if not all(isinstance(item, dict) for item in data):
            return self._build_fault(
                payload,
                code="public_ws_malformed_envelope",
                detail="public websocket envelope items must be objects",
            )
        symbol = arg.get("instId")
        if not isinstance(symbol, str) or not symbol.strip():
            return self._build_fault(
                payload,
                code="public_ws_malformed_envelope",
                detail="public websocket envelope instId must be a non-empty string",
            )
        return (channel.strip(), symbol.strip(), data)

    def _build_fault(
        self,
        payload: dict[str, Any],
        *,
        code: str | None = None,
        detail: str | None = None,
    ) -> FaultEvent:
        return FaultEvent(
            event_type=TraderEventType.RUNTIME_FAULT,
            exchange="okx",
            generated_at=datetime.now(UTC),
            severity="warn",
            code=str(code or payload.get("code") or "public_ws_error"),
            detail=(detail or str(payload.get("msg") or "public websocket fault")).strip(),
        )

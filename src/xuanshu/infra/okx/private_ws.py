from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from xuanshu.contracts.events import (
    AccountSnapshotEvent,
    FaultEvent,
    OrderUpdateEvent,
    PositionUpdateEvent,
)
from xuanshu.core.enums import TraderEventType


@dataclass(frozen=True, slots=True)
class OkxPrivateStream:
    url: str

    def build_login_payload(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        epoch_seconds: int,
    ) -> dict[str, object]:
        prehash = f"{epoch_seconds}GET/users/self/verify".encode()
        signature = base64.b64encode(
            hmac.new(api_secret.encode(), prehash, hashlib.sha256).digest()
        ).decode()
        return {
            "op": "login",
            "args": [
                {
                    "apiKey": api_key,
                    "passphrase": passphrase,
                    "timestamp": str(epoch_seconds),
                    "sign": signature,
                }
            ],
        }

    def decode_message(
        self,
        payload: dict[str, Any],
        sequence: str,
    ) -> tuple[OrderUpdateEvent | PositionUpdateEvent | AccountSnapshotEvent | FaultEvent, ...]:
        event = payload.get("event")
        if event == "login":
            if str(payload.get("code", "0")) == "0":
                return ()
            return (self._build_fault(payload, code=str(payload.get("code") or "login_failed")),)
        if event == "error":
            return (self._build_fault(payload, code=str(payload.get("code") or "private_ws_error")),)

        envelope = self._normalize_envelope(payload)
        if isinstance(envelope, FaultEvent):
            return (envelope,)

        channel, data = envelope
        if not data:
            return ()
        if channel not in {"orders", "positions", "account"}:
            return (
                self._build_fault(
                    payload,
                    code="private_ws_unknown_channel",
                    detail=f"unknown private channel: {channel}",
                ),
            )

        events: list[OrderUpdateEvent | PositionUpdateEvent | AccountSnapshotEvent | FaultEvent] = []
        for item in data:
            try:
                if channel == "orders":
                    events.append(self._decode_order(item, sequence))
                    continue
                if channel == "positions":
                    events.append(self._decode_position(item, sequence))
                    continue
                if channel == "account":
                    events.append(self._decode_account(item, sequence))
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                events.append(self._build_fault(payload, code=f"{channel}_decode_error", detail=str(exc)))
        return tuple(events)

    def _normalize_envelope(
        self, payload: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]]] | FaultEvent:
        arg = payload.get("arg")
        if not isinstance(arg, dict):
            return self._build_fault(
                payload,
                code="private_ws_malformed_envelope",
                detail="private websocket envelope arg must be an object",
            )

        channel = arg.get("channel")
        if not isinstance(channel, str) or not channel.strip():
            return self._build_fault(
                payload,
                code="private_ws_malformed_envelope",
                detail="private websocket envelope channel must be a non-empty string",
            )

        data = payload.get("data")
        if data is None:
            return (channel.strip(), [])
        if not isinstance(data, list):
            return self._build_fault(
                payload,
                code="private_ws_malformed_envelope",
                detail="private websocket envelope data must be a list",
            )
        if not all(isinstance(item, dict) for item in data):
            return self._build_fault(
                payload,
                code="private_ws_malformed_envelope",
                detail="private websocket envelope items must be objects",
            )
        return (channel.strip(), data)

    def _decode_order(self, item: dict[str, Any], sequence: str) -> OrderUpdateEvent:
        generated_at = self._parse_timestamp(item["uTime"])
        order_id = self._required_str(item["ordId"], field="ordId")
        client_order_id = self._optional_str(item.get("clOrdId"), default=order_id)
        return OrderUpdateEvent(
            event_type=TraderEventType.ORDER_UPDATE,
            symbol=self._required_str(item["instId"], field="instId"),
            exchange="okx",
            generated_at=generated_at,
            private_sequence=sequence,
            order_id=order_id,
            client_order_id=client_order_id,
            side=self._required_str(item["side"], field="side"),
            price=self._optional_float(item.get("px"), default=0.0),
            size=self._required_float(item["sz"], field="sz"),
            filled_size=self._optional_float(item.get("accFillSz"), default=0.0),
            status=self._required_str(item["state"], field="state"),
        )

    def _decode_position(self, item: dict[str, Any], sequence: str) -> PositionUpdateEvent:
        return PositionUpdateEvent(
            event_type=TraderEventType.POSITION_UPDATE,
            symbol=self._required_str(item["instId"], field="instId"),
            exchange="okx",
            generated_at=self._parse_timestamp(item["uTime"]),
            private_sequence=sequence,
            net_quantity=self._required_float(item["pos"], field="pos"),
            average_price=self._required_float(item["avgPx"], field="avgPx"),
            mark_price=self._required_float(item["markPx"], field="markPx"),
            unrealized_pnl=self._required_float(item["upl"], field="upl"),
        )

    def _decode_account(self, item: dict[str, Any], sequence: str) -> AccountSnapshotEvent:
        return AccountSnapshotEvent(
            event_type=TraderEventType.ACCOUNT_SNAPSHOT,
            exchange="okx",
            generated_at=self._parse_timestamp(item["uTime"]),
            private_sequence=sequence,
            equity=self._required_float(item["totalEq"], field="totalEq"),
            available_balance=self._required_float(item["availEq"], field="availEq"),
            margin_ratio=self._required_float(item["mgnRatio"], field="mgnRatio"),
        )

    def _build_fault(
        self,
        payload: dict[str, Any],
        *,
        code: str,
        detail: str | None = None,
    ) -> FaultEvent:
        msg = str(payload.get("msg") or detail or "private websocket fault").strip()
        conn_id = str(payload.get("connId") or "").strip()
        if conn_id:
            msg = f"{msg} (connId={conn_id})"
        return FaultEvent(
            event_type=TraderEventType.RUNTIME_FAULT,
            exchange="okx",
            generated_at=datetime.now(UTC),
            severity="critical",
            code=code,
            detail=msg,
        )

    def _parse_timestamp(self, value: Any) -> datetime:
        return datetime.fromtimestamp(int(str(value).strip()) / 1000, tz=UTC)

    def _required_str(self, value: Any, *, field: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{field} is required")
        return normalized

    def _optional_str(self, value: Any, *, default: str) -> str:
        normalized = str(value or "").strip()
        return normalized or default

    def _required_float(self, value: Any, *, field: str) -> float:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{field} is required")
        return float(normalized)

    def _optional_float(self, value: Any, *, default: float) -> float:
        normalized = str(value or "").strip()
        if not normalized:
            return default
        return float(normalized)

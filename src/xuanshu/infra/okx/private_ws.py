from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from xuanshu.contracts.events import AccountSnapshotEvent, OrderUpdateEvent, PositionUpdateEvent
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
    ) -> OrderUpdateEvent | PositionUpdateEvent | AccountSnapshotEvent | None:
        channel = payload.get("arg", {}).get("channel")
        data = payload.get("data") or []
        if not data:
            return None
        item = data[0]
        generated_at = datetime.fromtimestamp(int(item["uTime"]) / 1000, tz=UTC)
        if channel == "orders":
            return OrderUpdateEvent(
                event_type=TraderEventType.ORDER_UPDATE,
                symbol=item["instId"],
                exchange="okx",
                generated_at=generated_at,
                private_sequence=sequence,
                order_id=item["ordId"],
                client_order_id=item["clOrdId"],
                side=item["side"],
                price=float(item["px"]),
                size=float(item["sz"]),
                filled_size=float(item["accFillSz"]),
                status=item["state"],
            )
        if channel == "positions":
            return PositionUpdateEvent(
                event_type=TraderEventType.POSITION_UPDATE,
                symbol=item["instId"],
                exchange="okx",
                generated_at=generated_at,
                private_sequence=sequence,
                net_quantity=float(item["pos"]),
                average_price=float(item["avgPx"]),
                mark_price=float(item["markPx"]),
                unrealized_pnl=float(item["upl"]),
            )
        if channel == "account":
            return AccountSnapshotEvent(
                event_type=TraderEventType.ACCOUNT_SNAPSHOT,
                exchange="okx",
                generated_at=generated_at,
                private_sequence=sequence,
                equity=float(item["totalEq"]),
                available_balance=float(item["availEq"]),
                margin_ratio=float(item.get("mgnRatio", 0.0)),
            )
        return None

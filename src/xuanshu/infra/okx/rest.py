from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import urlencode

import httpx

_SUPPORTED_ORDER_TYPES = frozenset({"market", "limit"})
_SUPPORTED_ORDER_SIDES = frozenset({"buy", "sell"})
_PLACE_ORDER_REQUIRED_FIELDS = frozenset({"instId", "tdMode", "side", "ordType", "sz", "clOrdId"})
_PLACE_ORDER_OPTIONAL_FIELDS = frozenset({"px"})
_PLACE_ORDER_ALLOWED_FIELDS = _PLACE_ORDER_REQUIRED_FIELDS | _PLACE_ORDER_OPTIONAL_FIELDS


class OkxBusinessError(RuntimeError):
    def __init__(self, code: str, message: str, payload: object) -> None:
        self.code = code
        self.message = message
        self.payload = payload
        super().__init__(f"OKX business error {code}: {message}")


class OkxRestClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str | None = None,
        passphrase: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        self._closed = False

    async def __aenter__(self) -> "OkxRestClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        await self.client.aclose()
        self._closed = True

    def build_signed_headers(
        self,
        method: str,
        path: str,
        body: str,
        timestamp: str,
    ) -> dict[str, str]:
        if not self.api_secret or not self.passphrase:
            raise ValueError("api_secret and passphrase are required for signed requests")
        message = f"{timestamp}{method.upper()}{path}{body}".encode()
        signature = base64.b64encode(
            hmac.new(self.api_secret.encode(), message, hashlib.sha256).digest()
        ).decode()
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-SIGN": signature,
            "Content-Type": "application/json",
        }

    def build_place_order_payload(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: str,
        client_order_id: str,
        price: str | None = None,
    ) -> dict[str, str]:
        self._validate_order_entry_fields(side=side, order_type=order_type, price=price)
        self._validate_non_blank_fields(
            {
                "instId": symbol,
                "sz": size,
                "clOrdId": client_order_id,
            }
        )
        if price is not None:
            self._validate_non_blank_fields({"price": price})

        payload = {
            "instId": symbol,
            "tdMode": "cross",
            "side": side,
            "ordType": order_type,
            "sz": size,
            "clOrdId": client_order_id,
        }
        if price is not None:
            payload["px"] = price
        return payload

    async def place_order(self, payload: dict[str, str], timestamp: str) -> list[dict[str, object]]:
        self._validate_place_order_payload(payload)
        body = json.dumps(payload, separators=(",", ":"))
        headers = self.build_signed_headers("POST", "/api/v5/trade/order", body, timestamp)
        response = await self.client.post("/api/v5/trade/order", content=body, headers=headers)
        response.raise_for_status()
        return self._extract_order_data_payload(response.json())

    async def fetch_open_orders(self, symbol: str, timestamp: str) -> list[dict[str, object]]:
        self._validate_non_blank_fields({"instId": symbol})
        path = self._build_query_path("/api/v5/trade/orders-pending", {"instId": symbol})
        return await self._signed_get(path, timestamp)

    async def fetch_positions(self, symbol: str, timestamp: str) -> list[dict[str, object]]:
        self._validate_non_blank_fields({"instId": symbol})
        path = self._build_query_path("/api/v5/account/positions", {"instId": symbol})
        return await self._signed_get(path, timestamp)

    async def fetch_account_summary(self, timestamp: str) -> list[dict[str, object]]:
        return await self._signed_get("/api/v5/account/balance", timestamp)

    async def _signed_get(self, path: str, timestamp: str) -> list[dict[str, object]]:
        headers = self.build_signed_headers("GET", path, "", timestamp)
        response = await self.client.get(path, headers=headers)
        response.raise_for_status()
        return self._extract_data_payload(response.json())

    def _build_query_path(self, path: str, params: dict[str, str]) -> str:
        return f"{path}?{urlencode(params)}"

    def _extract_data_payload(self, payload: object) -> list[dict[str, object]]:
        payload_object = self._validate_payload_object(payload)
        self._raise_for_business_error(payload_object)
        data = self._extract_data_list(payload_object)
        if any(not isinstance(item, dict) for item in data):
            raise ValueError("OKX response payload data items must be objects")
        return data

    def _extract_order_data_payload(self, payload: object) -> list[dict[str, object]]:
        payload_object = self._validate_payload_object(payload)
        self._raise_for_business_error(payload_object)
        data = self._extract_data_list(payload_object)
        if any(not isinstance(item, dict) for item in data):
            raise ValueError("OKX response payload data items must be objects")
        for item in data:
            status_code = item.get("sCode")
            if status_code is not None and str(status_code) != "0":
                raise OkxBusinessError(
                    code=str(status_code),
                    message=str(item.get("sMsg") or payload_object.get("msg") or "order rejected"),
                    payload=item,
                )
        return data

    def _validate_payload_object(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("OKX response payload must be an object")
        return payload

    def _extract_data_list(self, payload: dict[str, object]) -> list[object]:
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("OKX response payload data must be a list")
        return data

    def _raise_for_business_error(self, payload: dict[str, object]) -> None:
        code = payload.get("code")
        if code is None:
            return
        if str(code) == "0":
            return
        raise OkxBusinessError(
            code=str(code),
            message=str(payload.get("msg") or "request rejected"),
            payload=payload,
        )

    def _validate_order_entry_fields(
        self,
        *,
        side: str,
        order_type: str,
        price: str | None,
    ) -> None:
        if side not in _SUPPORTED_ORDER_SIDES:
            raise ValueError(f"unsupported side: {side}")
        if order_type not in _SUPPORTED_ORDER_TYPES:
            raise ValueError(f"unsupported order_type: {order_type}")
        if order_type == "limit" and price is None:
            raise ValueError("price is required for limit orders")
        if order_type == "market" and price is not None:
            raise ValueError("price is not allowed for market orders")

    def _validate_place_order_payload(self, payload: dict[str, str]) -> None:
        missing_fields = sorted(_PLACE_ORDER_REQUIRED_FIELDS - payload.keys())
        if missing_fields:
            raise ValueError(f"missing required place_order payload fields: {missing_fields}")
        unexpected_fields = sorted(payload.keys() - _PLACE_ORDER_ALLOWED_FIELDS)
        if unexpected_fields:
            raise ValueError(
                f"unexpected place_order payload fields: {unexpected_fields}"
            )
        self._validate_non_blank_fields(
            {
                "instId": payload["instId"],
                "sz": payload["sz"],
                "clOrdId": payload["clOrdId"],
            }
        )
        if payload["tdMode"] != "cross":
            raise ValueError(f"unsupported tdMode: {payload['tdMode']}")
        self._validate_order_entry_fields(
            side=payload["side"],
            order_type=payload["ordType"],
            price=payload.get("px"),
        )
        if "px" in payload:
            self._validate_non_blank_fields({"price": payload["px"]})

    def _validate_non_blank_fields(self, fields: dict[str, str]) -> None:
        for field_name, value in fields.items():
            if not value.strip():
                raise ValueError(f"blank {field_name} is not allowed")

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from urllib.parse import urlencode

import httpx

_SUPPORTED_ORDER_TYPES = frozenset({"market", "limit"})
_SUPPORTED_ORDER_SIDES = frozenset({"buy", "sell"})
_SUPPORTED_MARGIN_MODES = frozenset({"cross", "isolated"})
_SUPPORTED_POSITION_SIDES = frozenset({"long", "short", "net"})
_PLACE_ORDER_REQUIRED_FIELDS = frozenset({"instId", "tdMode", "side", "posSide", "ordType", "sz", "clOrdId"})
_PLACE_ORDER_OPTIONAL_FIELDS = frozenset({"px", "reduceOnly"})
_PLACE_ORDER_ALLOWED_FIELDS = _PLACE_ORDER_REQUIRED_FIELDS | _PLACE_ORDER_OPTIONAL_FIELDS
_TRANSFER_ACCOUNT_TYPES = frozenset({"6", "18"})
_TRANSFER_TYPES = frozenset({"0", "1", "2", "3", "4"})


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
        simulated_trading: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.simulated_trading = simulated_trading
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
            **({"x-simulated-trading": "1"} if self.simulated_trading else {}),
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
            "posSide": "long" if side == "buy" else "short",
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

    async def set_leverage(
        self,
        *,
        symbol: str,
        leverage: int,
        margin_mode: str,
        position_side: str | None,
        timestamp: str,
    ) -> list[dict[str, object]]:
        payload = self.build_set_leverage_payload(
            symbol=symbol,
            leverage=leverage,
            margin_mode=margin_mode,
            position_side=position_side,
        )
        body = json.dumps(payload, separators=(",", ":"))
        headers = self.build_signed_headers("POST", "/api/v5/account/set-leverage", body, timestamp)
        response = await self.client.post("/api/v5/account/set-leverage", content=body, headers=headers)
        response.raise_for_status()
        return self._extract_data_payload(response.json())

    def build_set_leverage_payload(
        self,
        *,
        symbol: str,
        leverage: int,
        margin_mode: str,
        position_side: str | None,
    ) -> dict[str, str]:
        self._validate_non_blank_fields({"instId": symbol, "mgnMode": margin_mode})
        if type(leverage) is not int or leverage < 1:
            raise ValueError(f"unsupported leverage: {leverage!r}")
        if margin_mode not in _SUPPORTED_MARGIN_MODES:
            raise ValueError(f"unsupported mgnMode: {margin_mode}")
        payload = {
            "instId": symbol,
            "lever": str(leverage),
            "mgnMode": margin_mode,
        }
        if position_side is not None:
            self._validate_non_blank_fields({"posSide": position_side})
            if position_side not in _SUPPORTED_POSITION_SIDES:
                raise ValueError(f"unsupported posSide: {position_side}")
            payload["posSide"] = position_side
        return payload

    async def transfer_funds(
        self,
        *,
        currency: str,
        amount: str,
        from_account: str,
        to_account: str,
        timestamp: str,
        transfer_type: str = "0",
        client_id: str | None = None,
    ) -> list[dict[str, object]]:
        payload = self.build_transfer_payload(
            currency=currency,
            amount=amount,
            from_account=from_account,
            to_account=to_account,
            transfer_type=transfer_type,
            client_id=client_id,
        )
        body = json.dumps(payload, separators=(",", ":"))
        headers = self.build_signed_headers("POST", "/api/v5/asset/transfer", body, timestamp)
        response = await self.client.post("/api/v5/asset/transfer", content=body, headers=headers)
        response.raise_for_status()
        return self._extract_data_payload(response.json())

    def build_transfer_payload(
        self,
        *,
        currency: str,
        amount: str,
        from_account: str,
        to_account: str,
        transfer_type: str = "0",
        client_id: str | None = None,
    ) -> dict[str, str]:
        self._validate_non_blank_fields(
            {
                "ccy": currency,
                "amt": amount,
                "from": from_account,
                "to": to_account,
                "type": transfer_type,
            }
        )
        if from_account not in _TRANSFER_ACCOUNT_TYPES:
            raise ValueError(f"unsupported transfer from account: {from_account}")
        if to_account not in _TRANSFER_ACCOUNT_TYPES:
            raise ValueError(f"unsupported transfer to account: {to_account}")
        if from_account == to_account:
            raise ValueError("transfer from and to accounts must differ")
        if transfer_type not in _TRANSFER_TYPES:
            raise ValueError(f"unsupported transfer type: {transfer_type}")
        amount_value = self._parse_positive_decimal_string(amount, field_name="amt")
        payload = {
            "ccy": currency.upper(),
            "amt": amount_value,
            "from": from_account,
            "to": to_account,
            "type": transfer_type,
        }
        if client_id is not None:
            self._validate_non_blank_fields({"clientId": client_id})
            payload["clientId"] = client_id
        return payload

    async def fetch_transfer_state(
        self,
        *,
        timestamp: str,
        transfer_id: str | None = None,
        client_id: str | None = None,
        transfer_type: str = "0",
    ) -> list[dict[str, object]]:
        params = {"type": transfer_type}
        if transfer_id is not None:
            self._validate_non_blank_fields({"transId": transfer_id})
            params["transId"] = transfer_id
        if client_id is not None:
            self._validate_non_blank_fields({"clientId": client_id})
            params["clientId"] = client_id
        if "transId" not in params and "clientId" not in params:
            raise ValueError("transfer_id or client_id is required")
        path = self._build_query_path("/api/v5/asset/transfer-state", params)
        return await self._signed_get(path, timestamp)

    async def fetch_history_candles(
        self,
        symbol: str,
        *,
        bar: str = "1H",
        after: str | None = None,
        before: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        self._validate_non_blank_fields({"instId": symbol, "bar": bar})
        if limit <= 0 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        params = {"instId": symbol, "bar": bar, "limit": str(limit)}
        if after is not None:
            self._validate_non_blank_fields({"after": after})
            params["after"] = after
        if before is not None:
            self._validate_non_blank_fields({"before": before})
            params["before"] = before
        path = self._build_query_path("/api/v5/market/history-candles", params)
        response = await self.client.get(path)
        response.raise_for_status()
        return self._extract_candle_data_payload(response.json())

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
        data = self._extract_data_list(payload_object)
        if any(not isinstance(item, dict) for item in data):
            raise ValueError("OKX response payload data items must be objects")
        for item in data:
            status_code = item.get("sCode")
            if status_code is not None and str(status_code) != "0":
                raise OkxBusinessError(
                    code=str(status_code),
                    message=str(
                        item.get("sMsg")
                        or payload_object.get("msg")
                        or "order rejected"
                    ),
                    payload={"response": payload_object, "item": item},
                )
        self._raise_for_business_error(payload_object)
        return data

    def _extract_candle_data_payload(self, payload: object) -> list[dict[str, object]]:
        payload_object = self._validate_payload_object(payload)
        self._raise_for_business_error(payload_object)
        data = self._extract_data_list(payload_object)
        normalized: list[dict[str, object]] = []
        for item in data:
            if not isinstance(item, list) or len(item) < 5:
                raise ValueError("OKX candle payload items must be arrays with at least 5 entries")
            normalized.append(
                {
                    "ts": item[0],
                    "open": item[1],
                    "high": item[2],
                    "low": item[3],
                    "close": item[4],
                }
            )
        return normalized

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
        if "reduceOnly" in payload and payload["reduceOnly"] not in {"true", "false"}:
            raise ValueError(f"unsupported reduceOnly: {payload['reduceOnly']}")

    def _validate_non_blank_fields(self, fields: dict[str, str]) -> None:
        for field_name, value in fields.items():
            if not value.strip():
                raise ValueError(f"blank {field_name} is not allowed")

    def _parse_positive_decimal_string(self, value: str, *, field_name: str) -> str:
        try:
            numeric = float(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a positive number") from exc
        if not math.isfinite(numeric) or numeric <= 0:
            raise ValueError(f"{field_name} must be a positive number")
        return f"{numeric:g}"

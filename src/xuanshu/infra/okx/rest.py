from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx


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

    async def place_order(self, payload: dict[str, str], timestamp: str) -> dict[str, object]:
        body = json.dumps(payload, separators=(",", ":"))
        headers = self.build_signed_headers("POST", "/api/v5/trade/order", body, timestamp)
        response = await self.client.post("/api/v5/trade/order", content=body, headers=headers)
        response.raise_for_status()
        return response.json()

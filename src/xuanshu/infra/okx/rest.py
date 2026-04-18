from __future__ import annotations

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

    async def aclose(self) -> None:
        await self.client.aclose()

from datetime import UTC, datetime

import pytest

from xuanshu.momentum.okx_history import fetch_okx_history_rows, normalize_okx_candle


class _FakeOkxClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def fetch_history_candles(
        self,
        symbol: str,
        *,
        bar: str,
        after: str | None = None,
        before: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        self.calls.append({"symbol": symbol, "bar": bar, "after": after, "before": before, "limit": limit})
        if len(self.calls) == 1:
            return [
                {"ts": "1700003600000", "open": "101", "high": "102", "low": "100", "close": "101.5"},
                {"ts": "1700000000000", "open": "100", "high": "101", "low": "99", "close": "100.5"},
            ]
        return []


def test_normalize_okx_candle_parses_timestamp_and_prices() -> None:
    row = normalize_okx_candle({"ts": "1700000000000", "open": "100", "high": "101", "low": "99", "close": "100.5"})

    assert row["timestamp"] == datetime.fromtimestamp(1700000000, tz=UTC)
    assert row["close"] == 100.5


@pytest.mark.asyncio
async def test_fetch_okx_history_rows_returns_sorted_unique_rows() -> None:
    client = _FakeOkxClient()

    rows = await fetch_okx_history_rows(client, symbol="BTC-USDT-SWAP", bar="1H", limit=200)

    assert [row["close"] for row in rows] == [100.5, 101.5]
    assert client.calls[0]["limit"] == 100

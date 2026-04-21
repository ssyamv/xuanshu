from __future__ import annotations

from datetime import UTC, datetime
import math
from typing import Protocol


class OkxHistoryClient(Protocol):
    async def fetch_history_candles(
        self,
        symbol: str,
        *,
        bar: str = "1H",
        after: str | None = None,
        before: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        ...


async def fetch_okx_history_rows(
    client: OkxHistoryClient,
    *,
    symbol: str,
    bar: str,
    limit: int,
) -> list[dict[str, object]]:
    if not symbol.strip():
        raise ValueError("symbol must not be blank")
    if not bar.strip():
        raise ValueError("bar must not be blank")
    if limit <= 0:
        raise ValueError("limit must be positive")

    rows_by_timestamp: dict[datetime, dict[str, object]] = {}
    after: str | None = None
    remaining = limit
    while remaining > 0:
        request_limit = min(100, remaining)
        batch = await client.fetch_history_candles(
            symbol,
            bar=bar,
            after=after,
            limit=request_limit,
        )
        if not batch:
            break
        normalized_batch = [normalize_okx_candle(item) for item in batch]
        for row in normalized_batch:
            rows_by_timestamp[row["timestamp"]] = row
        remaining = limit - len(rows_by_timestamp)
        oldest_ts = min(str(item["ts"]) for item in batch if "ts" in item)
        if after == oldest_ts:
            break
        after = oldest_ts
        if len(batch) < request_limit:
            break

    return [rows_by_timestamp[key] for key in sorted(rows_by_timestamp)]


def normalize_okx_candle(row: dict[str, object]) -> dict[str, object]:
    timestamp = _parse_timestamp_ms(row.get("ts"))
    return {
        "timestamp": timestamp,
        "open": _parse_positive_float(row.get("open"), field_name="open"),
        "high": _parse_positive_float(row.get("high"), field_name="high"),
        "low": _parse_positive_float(row.get("low"), field_name="low"),
        "close": _parse_positive_float(row.get("close"), field_name="close"),
    }


def _parse_timestamp_ms(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("OKX candle ts must be a non-empty string")
    try:
        timestamp_ms = int(value)
    except ValueError as exc:
        raise ValueError("OKX candle ts must be an integer millisecond string") from exc
    if timestamp_ms <= 0:
        raise ValueError("OKX candle ts must be positive")
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)


def _parse_positive_float(value: object, *, field_name: str) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"OKX candle {field_name} must be a non-empty string")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"OKX candle {field_name} must be numeric") from exc
    if parsed <= 0 or not math.isfinite(parsed):
        raise ValueError(f"OKX candle {field_name} must be finite and positive")
    return parsed

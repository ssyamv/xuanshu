from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import re

from xuanshu.contracts.strategy import ApprovedStrategyBinding, StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.infra.okx.rest import OkxRestClient

_FIXED_VOL_BREAKOUT_PATTERN = re.compile(
    r"(?:^|-)vol-breakout-.+-(?P<bar>\d+[mhd])-k(?P<k>\d+)-ta(?P<trailing>\d+)-h(?P<hold>\d+)-atr(?P<atr>\d+)-ema(?P<ema>\d+)(?:-|$)",
    re.IGNORECASE,
)
_OKX_REST_BASE_URL = "https://www.okx.com"


@dataclass(frozen=True, slots=True)
class FixedVolBreakoutParameters:
    bar: str
    k: float
    trailing_atr: float
    max_hold_bars: int
    atr_period: int
    ema_period: int


class EntryGapReporter:
    def __init__(self, okx_rest_client: OkxRestClient) -> None:
        self._okx_rest_client = okx_rest_client

    async def render(self, *, snapshot: object | None, symbols: tuple[str, ...]) -> str:
        if not isinstance(snapshot, StrategyConfigSnapshot):
            return "开仓差距：暂无有效策略快照"
        lines = [
            "开仓条件差距：",
            f"快照：{snapshot.version_id}",
            f"模式：{snapshot.market_mode.value}；审批：{snapshot.approval_state.value}",
        ]
        if not snapshot.is_strategy_enabled("vol_breakout"):
            lines.append("vol_breakout 当前未启用，系统不会按波动率突破开仓。")
            return "\n".join(lines)
        if snapshot.market_mode in {RunMode.REDUCE_ONLY, RunMode.HALTED}:
            lines.append("当前模式阻止新开仓。")
        if snapshot.approval_state != ApprovalState.APPROVED:
            lines.append("当前快照未批准，阻止新开仓。")

        for symbol in symbols:
            binding = snapshot.strategy_binding_for(symbol, "vol_breakout")
            if binding is None:
                lines.append(f"{symbol}: 无 vol_breakout 绑定")
                continue
            parameters = _parse_fixed_vol_breakout_parameters(binding)
            if parameters is None:
                lines.append(f"{symbol}: 无法解析策略参数 {binding.strategy_def_id}")
                continue
            try:
                metric = await self._compute_symbol_gap(symbol=symbol, parameters=parameters)
            except Exception as exc:
                lines.append(f"{symbol}: 行情计算失败：{exc}")
                continue
            lines.append(_format_symbol_gap(symbol, parameters, metric))
        return "\n".join(lines)

    async def _compute_symbol_gap(
        self,
        *,
        symbol: str,
        parameters: FixedVolBreakoutParameters,
    ) -> dict[str, object]:
        rows = await _fetch_fixed_vol_breakout_rows(self._okx_rest_client, symbol=symbol, parameters=parameters)
        warmup = max(parameters.ema_period, parameters.atr_period + 1)
        if len(rows) <= warmup:
            raise ValueError(f"历史K线不足：{len(rows)}/{warmup + 1}")
        closes = [float(row["close"]) for row in rows]
        highs = [float(row["high"]) for row in rows]
        lows = [float(row["low"]) for row in rows]
        ema_values = _ema(closes, parameters.ema_period)
        atr_values = _atr(highs=highs, lows=lows, closes=closes, period=parameters.atr_period)
        index = len(rows) - 1
        close = closes[index]
        ema = ema_values[index]
        breakout_level = closes[index - 1] + parameters.k * atr_values[index - 1]
        required_close = max(ema, breakout_level)
        return {
            "timestamp": rows[index]["timestamp"],
            "close": close,
            "ema": ema,
            "previous_close": closes[index - 1],
            "previous_atr": atr_values[index - 1],
            "breakout_level": breakout_level,
            "required_close": required_close,
            "gap_abs": max(required_close - close, 0.0),
            "gap_pct": max(((required_close / close) - 1.0) * 100, 0.0) if close else None,
            "ema_condition": close > ema,
            "breakout_condition": close > breakout_level,
        }


def build_entry_gap_reporter() -> EntryGapReporter:
    return EntryGapReporter(OkxRestClient(base_url=_OKX_REST_BASE_URL, api_key=""))


def load_fixed_strategy_snapshot(path: str | None) -> StrategyConfigSnapshot | None:
    if path is None or not path.strip():
        return None
    try:
        return StrategyConfigSnapshot.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _format_symbol_gap(
    symbol: str,
    parameters: FixedVolBreakoutParameters,
    metric: dict[str, object],
) -> str:
    gap_pct = metric["gap_pct"]
    gap_pct_text = "n/a" if gap_pct is None else f"{float(gap_pct):.2f}%"
    return (
        f"{symbol}: close={float(metric['close']):.2f}，"
        f"EMA{parameters.ema_period}={float(metric['ema']):.2f}（{'已满足' if metric['ema_condition'] else '未满足'}），"
        f"突破价={float(metric['breakout_level']):.2f}（{'已满足' if metric['breakout_condition'] else '未满足'}），"
        f"还差={float(metric['gap_abs']):.2f} / {gap_pct_text}；"
        f"公式=前收 {float(metric['previous_close']):.2f} + k{parameters.k:g} * ATR{parameters.atr_period} {float(metric['previous_atr']):.2f}"
    )


def _decode_compact_decimal(value: str) -> float:
    if len(value) <= 1:
        return float(value)
    if value.startswith("0"):
        return int(value) / 10
    if len(value) == 2:
        return int(value) / 10
    return int(value) / 100


def _parse_fixed_vol_breakout_parameters(binding: ApprovedStrategyBinding) -> FixedVolBreakoutParameters | None:
    match = _FIXED_VOL_BREAKOUT_PATTERN.search(binding.strategy_def_id)
    if match is None:
        return None
    return FixedVolBreakoutParameters(
        bar=match.group("bar").upper(),
        k=_decode_compact_decimal(match.group("k")),
        trailing_atr=_decode_compact_decimal(match.group("trailing")),
        max_hold_bars=int(match.group("hold")),
        atr_period=int(match.group("atr")),
        ema_period=int(match.group("ema")),
    )


def _fixed_vol_breakout_rows_limit(parameters: FixedVolBreakoutParameters) -> int:
    warmup = max(parameters.ema_period, parameters.atr_period + 1)
    return min(max(warmup + 3, 120), 300)


async def _fetch_fixed_vol_breakout_rows(
    client: OkxRestClient,
    *,
    symbol: str,
    parameters: FixedVolBreakoutParameters,
) -> list[dict[str, object]]:
    limit = _fixed_vol_breakout_rows_limit(parameters)
    rows_by_timestamp: dict[datetime, dict[str, object]] = {}
    after: str | None = None
    remaining = limit
    while remaining > 0:
        request_limit = min(100, remaining)
        batch = await client.fetch_history_candles(symbol, bar=parameters.bar, after=after, limit=request_limit)
        if not batch:
            break
        for item in batch:
            row = _normalize_okx_candle(item)
            rows_by_timestamp[row["timestamp"]] = row
        remaining = limit - len(rows_by_timestamp)
        oldest_ts = min(str(item["ts"]) for item in batch if "ts" in item)
        if after == oldest_ts or len(batch) < request_limit:
            break
        after = oldest_ts
    return [rows_by_timestamp[key] for key in sorted(rows_by_timestamp)]


def _normalize_okx_candle(row: dict[str, object]) -> dict[str, object]:
    timestamp = row.get("ts")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise ValueError("OKX candle timestamp is missing")
    return {
        "timestamp": datetime.fromtimestamp(int(timestamp) / 1000, tz=UTC),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
    }


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def _atr(*, highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float]:
    true_ranges: list[float] = []
    previous_close: float | None = None
    for high, low, close in zip(highs, lows, closes, strict=True):
        true_range = high - low if previous_close is None else max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )
        true_ranges.append(true_range)
        previous_close = close
    return _ema(true_ranges, period)

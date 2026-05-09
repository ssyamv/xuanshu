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
_FIXED_VOTE_TREND_PATTERN = re.compile(
    r"(?:^|-)vote-trend-.+-(?P<bar>\d+[mhd])-f(?P<fast>\d+)-s(?P<slow>\d+)"
    r"-lb(?P<lookback>\d+)-ch(?P<channel>\d+)-th(?P<threshold>\d+)-v(?P<votes>\d+)"
    r"-sl(?P<stop>\d+)-tp(?P<take>\d+)-h(?P<hold>\d+)(?:-(?P<mode>both|longonly))?(?:-|$)",
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


@dataclass(frozen=True, slots=True)
class FixedVoteTrendParameters:
    bar: str
    fast_ema_period: int
    slow_ema_period: int
    lookback_bars: int
    channel_bars: int
    threshold_bps: int
    required_votes: int
    stop_loss_bps: int
    take_profit_bps: int
    max_hold_bars: int
    allow_short: bool


class EntryGapReporter:
    def __init__(self, okx_rest_client: OkxRestClient) -> None:
        self._okx_rest_client = okx_rest_client

    async def render(self, *, snapshot: object | None, symbols: tuple[str, ...]) -> str:
        if not isinstance(snapshot, StrategyConfigSnapshot):
            return "开仓差距：暂无有效策略快照"
        lines = [
            "开仓条件差距",
            f"快照：{snapshot.version_id}",
            f"状态：模式 {snapshot.market_mode.value}；审批 {snapshot.approval_state.value}",
        ]
        if snapshot.market_mode in {RunMode.REDUCE_ONLY, RunMode.HALTED}:
            lines.append("当前模式阻止新开仓。")
        if snapshot.approval_state != ApprovalState.APPROVED:
            lines.append("当前快照未批准，阻止新开仓。")
        if snapshot.is_strategy_enabled("vote_trend"):
            return await self._render_vote_trend(lines=lines, snapshot=snapshot, symbols=symbols)
        if not snapshot.is_strategy_enabled("vol_breakout"):
            lines.append("当前快照未启用 /entrygap 支持的固定策略。")
            return "\n".join(lines)
        return await self._render_vol_breakout(lines=lines, snapshot=snapshot, symbols=symbols)

    async def _render_vol_breakout(
        self,
        *,
        lines: list[str],
        snapshot: StrategyConfigSnapshot,
        symbols: tuple[str, ...],
    ) -> str:
        reports: list[str] = []
        metrics: list[tuple[str, dict[str, object]]] = []
        for symbol in symbols:
            binding = snapshot.strategy_binding_for(symbol, "vol_breakout")
            if binding is None:
                reports.append(f"{symbol}\n- 状态：无 vol_breakout 绑定")
                continue
            parameters = _parse_fixed_vol_breakout_parameters(binding)
            if parameters is None:
                reports.append(f"{symbol}\n- 状态：无法解析策略参数 {binding.strategy_def_id}")
                continue
            try:
                metric = await self._compute_symbol_gap(symbol=symbol, parameters=parameters)
            except Exception as exc:
                reports.append(f"{symbol}\n- 状态：行情计算失败：{exc}")
                continue
            metrics.append((symbol, metric))
            reports.append(_format_symbol_gap(symbol, parameters, metric))
        summary = _format_entry_summary(metrics)
        if summary is not None:
            lines.append(summary)
        if reports:
            lines.extend(["", "\n\n".join(reports)])
        return "\n".join(lines)

    async def _render_vote_trend(
        self,
        *,
        lines: list[str],
        snapshot: StrategyConfigSnapshot,
        symbols: tuple[str, ...],
    ) -> str:
        reports: list[str] = []
        metrics: list[tuple[str, dict[str, object]]] = []
        for symbol in symbols:
            binding = snapshot.strategy_binding_for(symbol, "vote_trend")
            if binding is None:
                reports.append(f"{symbol}\n- 状态：无 vote_trend 绑定")
                continue
            parameters = _parse_fixed_vote_trend_parameters(binding)
            if parameters is None:
                reports.append(f"{symbol}\n- 状态：无法解析策略参数 {binding.strategy_def_id}")
                continue
            try:
                metric = await self._compute_vote_trend_gap(symbol=symbol, parameters=parameters)
            except Exception as exc:
                reports.append(f"{symbol}\n- 状态：行情计算失败：{exc}")
                continue
            metrics.append((symbol, metric))
            reports.append(_format_vote_trend_gap(symbol, parameters, metric))
        summary = _format_vote_trend_summary(metrics)
        if summary is not None:
            lines.append(summary)
        if reports:
            lines.extend(["", "\n\n".join(reports)])
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

    async def _compute_vote_trend_gap(
        self,
        *,
        symbol: str,
        parameters: FixedVoteTrendParameters,
    ) -> dict[str, object]:
        rows = await _fetch_fixed_vote_trend_rows(self._okx_rest_client, symbol=symbol, parameters=parameters)
        warmup = max(parameters.slow_ema_period, parameters.lookback_bars, parameters.channel_bars, 14)
        if len(rows) <= warmup:
            raise ValueError(f"历史K线不足：{len(rows)}/{warmup + 1}")
        closes = [float(row["close"]) for row in rows]
        highs = [float(row["high"]) for row in rows]
        lows = [float(row["low"]) for row in rows]
        fast_ema = _ema(closes, parameters.fast_ema_period)
        slow_ema = _ema(closes, parameters.slow_ema_period)
        rsi_values = _rsi(closes, 14)
        index = len(rows) - 1
        close = closes[index]
        lookback_close = closes[index - parameters.lookback_bars]
        threshold = parameters.threshold_bps / 10_000
        channel_high = max(highs[index - parameters.channel_bars : index])
        channel_low = min(lows[index - parameters.channel_bars : index])
        momentum = (close / lookback_close) - 1.0
        long_required = {
            "slow_ema": slow_ema[index],
            "momentum_close": lookback_close * (1 + threshold),
            "channel": channel_high,
            "rsi": 50.0,
        }
        short_required = {
            "slow_ema": slow_ema[index],
            "momentum_close": lookback_close * (1 - threshold),
            "channel": channel_low,
            "rsi": 50.0,
        }
        long_conditions = {
            "price_above_slow": close > slow_ema[index],
            "fast_above_slow": fast_ema[index] > slow_ema[index],
            "momentum_positive": momentum > threshold,
            "channel_breakout": close > channel_high,
            "rsi_above_50": rsi_values[index] > 50,
        }
        short_conditions = {
            "price_below_slow": close < slow_ema[index],
            "fast_below_slow": fast_ema[index] < slow_ema[index],
            "momentum_negative": momentum < -threshold,
            "channel_breakdown": close < channel_low,
            "rsi_below_50": rsi_values[index] < 50,
        }
        return {
            "timestamp": rows[index]["timestamp"],
            "close": close,
            "fast_ema": fast_ema[index],
            "slow_ema": slow_ema[index],
            "lookback_close": lookback_close,
            "momentum": momentum,
            "channel_high": channel_high,
            "channel_low": channel_low,
            "rsi": rsi_values[index],
            "long_conditions": long_conditions,
            "short_conditions": short_conditions,
            "required_votes": parameters.required_votes,
            "long_votes": sum(long_conditions.values()),
            "short_votes": sum(short_conditions.values()),
            "long_price_gap_abs": max(max(long_required["slow_ema"], long_required["momentum_close"], long_required["channel"]) - close, 0.0),
            "long_price_gap_pct": _positive_gap_pct(max(long_required["slow_ema"], long_required["momentum_close"], long_required["channel"]), close),
            "short_price_gap_abs": max(close - min(short_required["slow_ema"], short_required["momentum_close"], short_required["channel"]), 0.0),
            "short_price_gap_pct": _downside_gap_pct(close, min(short_required["slow_ema"], short_required["momentum_close"], short_required["channel"])),
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
    entry_condition = bool(metric["ema_condition"]) and bool(metric["breakout_condition"])
    gap_pct = metric["gap_pct"]
    gap_pct_text = "n/a" if gap_pct is None else f"{float(gap_pct):.2f}%"
    return "\n".join(
        [
            f"{symbol}（{'价格条件已满足' if entry_condition else '价格条件未满足'}）",
            f"- 现价：{float(metric['close']):.2f}",
            f"- 趋势：EMA{parameters.ema_period} {float(metric['ema']):.2f}，{'已满足' if metric['ema_condition'] else '未满足'}",
            f"- 突破：目标 {float(metric['breakout_level']):.2f}，{'已满足' if metric['breakout_condition'] else '未满足'}",
            f"- 差距：{float(metric['gap_abs']):.2f}（{gap_pct_text}）",
            (
                f"- 计算：前收 {float(metric['previous_close']):.2f} + "
                f"k{parameters.k:g} * ATR{parameters.atr_period} {float(metric['previous_atr']):.2f}"
            ),
        ]
    )


def _format_entry_summary(metrics: list[tuple[str, dict[str, object]]]) -> str | None:
    if not metrics:
        return None
    satisfied_count = sum(
        1 for _, metric in metrics if bool(metric["ema_condition"]) and bool(metric["breakout_condition"])
    )
    if satisfied_count == len(metrics):
        return f"结论：价格条件 {satisfied_count}/{len(metrics)} 已满足；所有可计算标的已达到开仓条件。"

    pending = [
        (symbol, float(metric["gap_abs"]), metric["gap_pct"])
        for symbol, metric in metrics
        if not (bool(metric["ema_condition"]) and bool(metric["breakout_condition"]))
    ]
    closest_symbol, closest_abs, closest_pct = min(
        pending,
        key=lambda item: (float("inf") if item[2] is None else float(item[2]), item[1]),
    )
    closest_pct_text = "n/a" if closest_pct is None else f"{float(closest_pct):.2f}%"
    return (
        f"结论：价格条件 {satisfied_count}/{len(metrics)} 已满足；"
        f"最近 {closest_symbol} 还差 {closest_abs:.2f}（{closest_pct_text}）。"
    )


def _format_vote_trend_gap(
    symbol: str,
    parameters: FixedVoteTrendParameters,
    metric: dict[str, object],
) -> str:
    long_votes = int(metric["long_votes"])
    short_votes = int(metric["short_votes"])
    required_votes = parameters.required_votes
    long_ready = long_votes >= required_votes
    short_ready = parameters.allow_short and short_votes >= required_votes
    long_gap_pct = float(metric["long_price_gap_pct"])
    short_gap_pct = float(metric["short_price_gap_pct"])
    lines = [
        f"{symbol}（{'已满足开仓条件' if long_ready or short_ready else '未满足开仓条件'}）",
        f"- 策略：vote_trend {parameters.bar}，需要 {required_votes}/5 票；{'允许做空' if parameters.allow_short else '仅做多'}",
        f"- 现价：{float(metric['close']):.2f}",
        f"- 投票：多头 {long_votes}/{required_votes}；空头 {short_votes}/{required_votes}",
        (
            f"- EMA：fast{parameters.fast_ema_period} {float(metric['fast_ema']):.2f} / "
            f"slow{parameters.slow_ema_period} {float(metric['slow_ema']):.2f}"
        ),
        f"- 动量：{float(metric['momentum']) * 100:.2f}%（回看 {parameters.lookback_bars} 根）",
        (
            f"- 通道：上沿 {float(metric['channel_high']):.2f} / "
            f"下沿 {float(metric['channel_low']):.2f}（{parameters.channel_bars} 根）"
        ),
        f"- RSI：{float(metric['rsi']):.2f}",
        f"- 多头价格差距：{float(metric['long_price_gap_abs']):.2f}（{long_gap_pct:.2f}%）",
    ]
    if parameters.allow_short:
        lines.append(f"- 空头价格差距：{float(metric['short_price_gap_abs']):.2f}（{short_gap_pct:.2f}%）")
    missing_long = _missing_vote_labels(metric["long_conditions"], _LONG_VOTE_LABELS)
    missing_short = _missing_vote_labels(metric["short_conditions"], _SHORT_VOTE_LABELS)
    if missing_long:
        lines.append(f"- 多头未满足：{missing_long}")
    if parameters.allow_short and missing_short:
        lines.append(f"- 空头未满足：{missing_short}")
    return "\n".join(lines)


def _format_vote_trend_summary(metrics: list[tuple[str, dict[str, object]]]) -> str | None:
    if not metrics:
        return None
    ready = [
        symbol
        for symbol, metric in metrics
        if int(metric["long_votes"]) >= int(metric["required_votes"])
        or int(metric["short_votes"]) >= int(metric["required_votes"])
    ]
    if ready:
        return f"结论：{', '.join(ready)} 当前投票已满足开仓条件。"
    closest_symbol, closest_metric = max(
        metrics,
        key=lambda item: max(int(item[1]["long_votes"]), int(item[1]["short_votes"])),
    )
    best_votes = max(int(closest_metric["long_votes"]), int(closest_metric["short_votes"]))
    return f"结论：当前未触发开仓；最近 {closest_symbol} 为 {best_votes}/{int(closest_metric['required_votes'])} 票。"


_LONG_VOTE_LABELS = {
    "price_above_slow": "价格高于慢 EMA",
    "fast_above_slow": "快 EMA 高于慢 EMA",
    "momentum_positive": "动量为正",
    "channel_breakout": "突破通道上沿",
    "rsi_above_50": "RSI 高于 50",
}
_SHORT_VOTE_LABELS = {
    "price_below_slow": "价格低于慢 EMA",
    "fast_below_slow": "快 EMA 低于慢 EMA",
    "momentum_negative": "动量为负",
    "channel_breakdown": "跌破通道下沿",
    "rsi_below_50": "RSI 低于 50",
}


def _missing_vote_labels(conditions: object, labels: dict[str, str]) -> str:
    if not isinstance(conditions, dict):
        return ""
    missing = [label for key, label in labels.items() if not bool(conditions.get(key))]
    return "、".join(missing)


def _positive_gap_pct(required: float, current: float) -> float:
    if current <= 0:
        return 0.0
    return max(((required / current) - 1.0) * 100, 0.0)


def _downside_gap_pct(current: float, target: float) -> float:
    if current <= 0:
        return 0.0
    return max(((current - target) / current) * 100, 0.0)


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


def _parse_fixed_vote_trend_parameters(binding: ApprovedStrategyBinding) -> FixedVoteTrendParameters | None:
    match = _FIXED_VOTE_TREND_PATTERN.search(binding.strategy_def_id)
    if match is None:
        return None
    return FixedVoteTrendParameters(
        bar=match.group("bar").upper(),
        fast_ema_period=int(match.group("fast")),
        slow_ema_period=int(match.group("slow")),
        lookback_bars=int(match.group("lookback")),
        channel_bars=int(match.group("channel")),
        threshold_bps=int(match.group("threshold")),
        required_votes=int(match.group("votes")),
        stop_loss_bps=int(match.group("stop")),
        take_profit_bps=int(match.group("take")),
        max_hold_bars=int(match.group("hold")),
        allow_short=(match.group("mode") or "both").lower() != "longonly",
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


def _fixed_vote_trend_rows_limit(parameters: FixedVoteTrendParameters) -> int:
    warmup = max(parameters.slow_ema_period, parameters.lookback_bars, parameters.channel_bars, 14)
    return min(max(warmup + parameters.max_hold_bars + 3, 240), 300)


async def _fetch_fixed_vote_trend_rows(
    client: OkxRestClient,
    *,
    symbol: str,
    parameters: FixedVoteTrendParameters,
) -> list[dict[str, object]]:
    limit = _fixed_vote_trend_rows_limit(parameters)
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


def _rsi(closes: list[float], period: int) -> list[float]:
    gains = [0.0]
    losses = [0.0]
    for previous, current in zip(closes, closes[1:], strict=False):
        delta = current - previous
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    average_gains = _ema(gains, period)
    average_losses = _ema(losses, period)
    output: list[float] = []
    for gain, loss in zip(average_gains, average_losses, strict=True):
        output.append(100.0 if loss == 0 else 100 - (100 / (1 + gain / loss)))
    return output

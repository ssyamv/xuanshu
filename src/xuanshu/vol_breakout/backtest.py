from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import math
from numbers import Real

from xuanshu.contracts.strategy import ApprovedStrategyBinding, StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode


@dataclass(frozen=True, slots=True)
class VolBreakoutParameters:
    k: float = 0.8
    trailing_atr: float = 2.5
    max_hold_bars: int = 12
    atr_period: int = 14
    ema_period: int = 200
    bar: str = "4H"

    def __post_init__(self) -> None:
        if self.k <= 0 or not math.isfinite(self.k):
            raise ValueError("k must be finite and positive")
        if self.trailing_atr <= 0 or not math.isfinite(self.trailing_atr):
            raise ValueError("trailing_atr must be finite and positive")
        if self.max_hold_bars <= 0:
            raise ValueError("max_hold_bars must be positive")
        if self.atr_period <= 0:
            raise ValueError("atr_period must be positive")
        if self.ema_period <= 0:
            raise ValueError("ema_period must be positive")


@dataclass(frozen=True, slots=True)
class VolBreakoutConfig:
    min_trade_count: int = 10
    max_drawdown_percent: float = 15.0
    risk_fraction: float = 0.25
    fee_bps: float = 4.0

    def __post_init__(self) -> None:
        if self.min_trade_count <= 0:
            raise ValueError("min_trade_count must be positive")
        if self.max_drawdown_percent <= 0 or not math.isfinite(self.max_drawdown_percent):
            raise ValueError("max_drawdown_percent must be finite and positive")
        if self.risk_fraction <= 0 or self.risk_fraction > 1 or not math.isfinite(self.risk_fraction):
            raise ValueError("risk_fraction must be > 0 and <= 1")
        if self.fee_bps < 0 or not math.isfinite(self.fee_bps):
            raise ValueError("fee_bps must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class VolBreakoutResult:
    parameters: VolBreakoutParameters
    sample_count: int
    trade_count: int
    return_percent: float
    max_drawdown_percent: float
    win_rate: float
    profit_factor: float
    stability_score: float


@dataclass(frozen=True, slots=True)
class _Position:
    entry_index: int
    entry_close: float
    highest_close: float


def evaluate_vol_breakout(
    parameters: VolBreakoutParameters,
    historical_rows: list[dict[str, object]],
    *,
    config: VolBreakoutConfig,
) -> VolBreakoutResult:
    rows = _normalize_rows(historical_rows)
    warmup = max(parameters.ema_period, parameters.atr_period + 1)
    if len(rows) <= warmup:
        return _empty_result(parameters=parameters, sample_count=len(rows))

    closes = [row["close"] for row in rows]
    highs = [row["high"] for row in rows]
    lows = [row["low"] for row in rows]
    ema_values = _ema(closes, parameters.ema_period)
    atr_values = _atr(highs=highs, lows=lows, closes=closes, period=parameters.atr_period)
    fee = config.fee_bps / 10_000
    trades: list[float] = []
    position: _Position | None = None

    for index in range(warmup, len(rows)):
        close = closes[index]
        if position is not None:
            highest_close = max(position.highest_close, close)
            trailing_stop = highest_close - parameters.trailing_atr * atr_values[index]
            max_hold_reached = index - position.entry_index >= parameters.max_hold_bars
            if close < trailing_stop or max_hold_reached:
                trades.append((close / position.entry_close) - 1.0 - 2 * fee)
                position = None
                continue
            position = _Position(
                entry_index=position.entry_index,
                entry_close=position.entry_close,
                highest_close=highest_close,
            )
            continue

        breakout_level = closes[index - 1] + parameters.k * atr_values[index - 1]
        if close > ema_values[index] and close > breakout_level and index < len(rows) - 1:
            position = _Position(entry_index=index, entry_close=close, highest_close=close)

    if position is not None:
        final_close = closes[-1]
        trades.append((final_close / position.entry_close) - 1.0 - 2 * fee)

    return _build_result(parameters=parameters, sample_count=len(rows), trade_returns=trades)


def candidate_passes(result: VolBreakoutResult, *, config: VolBreakoutConfig) -> bool:
    return (
        result.trade_count >= config.min_trade_count
        and result.return_percent > 0
        and result.profit_factor > 1
        and result.max_drawdown_percent <= config.max_drawdown_percent
    )


def build_vol_breakout_snapshot(
    *,
    selected: VolBreakoutResult,
    symbol: str,
    generated_at: datetime,
    config: VolBreakoutConfig,
) -> StrategyConfigSnapshot:
    normalized_time = _normalize_timestamp(generated_at)
    strategy_def_id = _build_strategy_def_id(symbol=symbol, parameters=selected.parameters)
    backtest_report_id = _build_backtest_report_id(symbol=symbol, selected=selected)
    package_id = f"fixed-{strategy_def_id}"
    return StrategyConfigSnapshot(
        version_id=f"fixed-{strategy_def_id}",
        generated_at=normalized_time,
        effective_from=normalized_time,
        expires_at=normalized_time + timedelta(days=3650),
        symbol_whitelist=[symbol],
        strategy_enable_flags={"vol_breakout": True},
        risk_multiplier=config.risk_fraction,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.HALTED,
        approval_state=ApprovalState.APPROVED,
        source_reason=f"fixed {symbol} {selected.parameters.bar} volatility breakout backtest",
        ttl_sec=315_360_000,
        symbol_strategy_bindings={
            symbol: ApprovedStrategyBinding(
                strategy_def_id=strategy_def_id,
                strategy_package_id=package_id,
                backtest_report_id=backtest_report_id,
                score=max(0.0, selected.return_percent),
                score_basis="backtest_return_percent",
                approval_record_id=package_id,
                activated_at=normalized_time,
            )
        },
    )


def _normalize_rows(rows: list[dict[str, object]]) -> list[dict[str, float | datetime]]:
    normalized = [
        {
            "timestamp": _extract_timestamp(row),
            "open": _extract_positive_float(row, "open"),
            "high": _extract_positive_float(row, "high"),
            "low": _extract_positive_float(row, "low"),
            "close": _extract_positive_float(row, "close"),
        }
        for row in rows
    ]
    normalized.sort(key=lambda row: row["timestamp"])
    for previous, current in zip(normalized, normalized[1:], strict=False):
        if previous["timestamp"] == current["timestamp"]:
            raise ValueError("historical rows must have unique timestamps")
    return normalized


def _extract_timestamp(row: dict[str, object]) -> datetime:
    timestamp = row.get("timestamp")
    if not isinstance(timestamp, datetime):
        raise ValueError("historical rows must include datetime timestamp")
    return _normalize_timestamp(timestamp)


def _extract_positive_float(row: dict[str, object], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"historical rows must include numeric {key}")
    parsed = float(value)
    if parsed <= 0 or not math.isfinite(parsed):
        raise ValueError(f"historical {key} must be finite and positive")
    return parsed


def _normalize_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


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
        if previous_close is None:
            true_range = high - low
        else:
            true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(true_range)
        previous_close = close
    return _ema(true_ranges, period)


def _build_result(
    *,
    parameters: VolBreakoutParameters,
    sample_count: int,
    trade_returns: list[float],
) -> VolBreakoutResult:
    trade_count = len(trade_returns)
    return_percent = round(sum(trade_returns) * 100, 6)
    max_drawdown_percent = round(_max_drawdown(trade_returns) * 100, 6)
    wins = [value for value in trade_returns if value > 0]
    losses = [value for value in trade_returns if value < 0]
    win_rate = round(len(wins) / trade_count, 6) if trade_count else 0.0
    profit_factor = _profit_factor(wins=wins, losses=losses)
    stability_score = _stability_score(
        return_percent=return_percent,
        max_drawdown_percent=max_drawdown_percent,
        trade_count=trade_count,
        profit_factor=profit_factor,
    )
    return VolBreakoutResult(
        parameters=parameters,
        sample_count=sample_count,
        trade_count=trade_count,
        return_percent=return_percent,
        max_drawdown_percent=max_drawdown_percent,
        win_rate=win_rate,
        profit_factor=profit_factor,
        stability_score=stability_score,
    )


def _empty_result(*, parameters: VolBreakoutParameters, sample_count: int) -> VolBreakoutResult:
    return VolBreakoutResult(
        parameters=parameters,
        sample_count=sample_count,
        trade_count=0,
        return_percent=0.0,
        max_drawdown_percent=0.0,
        win_rate=0.0,
        profit_factor=0.0,
        stability_score=0.0,
    )


def _max_drawdown(trade_returns: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade_return in trade_returns:
        equity += trade_return
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return max_drawdown


def _profit_factor(*, wins: list[float], losses: list[float]) -> float:
    total_loss = abs(sum(losses))
    if not wins:
        return 0.0
    if total_loss == 0:
        return 999.0
    return round(sum(wins) / total_loss, 6)


def _stability_score(
    *,
    return_percent: float,
    max_drawdown_percent: float,
    trade_count: int,
    profit_factor: float,
) -> float:
    if trade_count == 0 or return_percent <= 0:
        return 0.0
    drawdown_penalty = 1 + max_drawdown_percent
    bounded_profit_factor = min(profit_factor, 10.0)
    return round((return_percent * bounded_profit_factor * min(trade_count, 100)) / (100 * drawdown_penalty), 6)


def _build_strategy_def_id(*, symbol: str, parameters: VolBreakoutParameters) -> str:
    normalized_symbol = symbol.lower()
    digest = _parameter_digest(symbol=symbol, parameters=parameters)
    return f"vol-breakout-{normalized_symbol}-{parameters.bar.lower()}-{digest}"


def _build_backtest_report_id(*, symbol: str, selected: VolBreakoutResult) -> str:
    digest = _parameter_digest(symbol=symbol, parameters=selected.parameters)
    return f"bt-vol-breakout-{digest}"


def _parameter_digest(*, symbol: str, parameters: VolBreakoutParameters) -> str:
    payload = (
        f"{symbol}|{parameters.bar}|{parameters.k}|{parameters.trailing_atr}|"
        f"{parameters.max_hold_bars}|{parameters.atr_period}|{parameters.ema_period}"
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:12]

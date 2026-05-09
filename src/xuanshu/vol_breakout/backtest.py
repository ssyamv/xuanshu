from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import math
from numbers import Real

from xuanshu.contracts.strategy import ApprovedStrategyBinding, StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.sizing.position_sizer import CONTRACT_VALUE_BY_SYMBOL, OpenOrderSizingInput, calculate_open_order_size


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
    symbol: str = "ETH-USDT-SWAP"
    initial_equity: float = 1_000.0
    initial_available_balance: float | None = None
    per_symbol_max_position: float = 0.12
    max_leverage: int = 3

    def __post_init__(self) -> None:
        if self.min_trade_count <= 0:
            raise ValueError("min_trade_count must be positive")
        if self.max_drawdown_percent <= 0 or not math.isfinite(self.max_drawdown_percent):
            raise ValueError("max_drawdown_percent must be finite and positive")
        if self.risk_fraction <= 0 or self.risk_fraction > 1 or not math.isfinite(self.risk_fraction):
            raise ValueError("risk_fraction must be > 0 and <= 1")
        if self.fee_bps < 0 or not math.isfinite(self.fee_bps):
            raise ValueError("fee_bps must be finite and non-negative")
        if self.initial_equity <= 0 or not math.isfinite(self.initial_equity):
            raise ValueError("initial_equity must be finite and positive")
        if self.initial_available_balance is not None and (
            self.initial_available_balance < 0 or not math.isfinite(self.initial_available_balance)
        ):
            raise ValueError("initial_available_balance must be finite and non-negative")
        if self.per_symbol_max_position < 0 or self.per_symbol_max_position > 1:
            raise ValueError("per_symbol_max_position must be >= 0 and <= 1")
        if self.max_leverage < 1:
            raise ValueError("max_leverage must be positive")


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
    initial_equity: float = 0.0
    final_equity: float = 0.0
    blocked_signal_count: int = 0


@dataclass(frozen=True, slots=True)
class _Position:
    entry_index: int
    entry_close: float
    highest_close: float
    size: float
    reserved_margin: float
    entry_fee: float


@dataclass(frozen=True, slots=True)
class _AccountState:
    equity: float
    available_balance: float


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
    fee_rate = config.fee_bps / 10_000
    contract_value = CONTRACT_VALUE_BY_SYMBOL.get(config.symbol)
    if contract_value is None:
        raise ValueError(f"unsupported contract symbol for vol_breakout backtest: {config.symbol}")
    initial_available_balance = (
        config.initial_equity if config.initial_available_balance is None else config.initial_available_balance
    )
    account = _AccountState(equity=config.initial_equity, available_balance=initial_available_balance)
    equity_points = [account.equity]
    trades: list[float] = []
    position: _Position | None = None
    blocked_signal_count = 0

    for index in range(warmup, len(rows)):
        close = closes[index]
        if position is not None:
            highest_close = max(position.highest_close, close)
            trailing_stop = highest_close - parameters.trailing_atr * atr_values[index]
            max_hold_reached = index - position.entry_index >= parameters.max_hold_bars
            if close < trailing_stop or max_hold_reached:
                trade_return, account = _close_position(
                    position=position,
                    close=close,
                    account=account,
                    contract_value=contract_value,
                    fee_rate=fee_rate,
                    initial_equity=config.initial_equity,
                )
                trades.append(trade_return)
                equity_points.append(account.equity)
                position = None
                continue
            position = _Position(
                entry_index=position.entry_index,
                entry_close=position.entry_close,
                highest_close=highest_close,
                size=position.size,
                reserved_margin=position.reserved_margin,
                entry_fee=position.entry_fee,
            )
            continue

        breakout_level = closes[index - 1] + parameters.k * atr_values[index - 1]
        if close > ema_values[index] and close > breakout_level and index < len(rows) - 1:
            requested_size = max(1.0, account.equity * config.per_symbol_max_position * config.risk_fraction)
            sizing = calculate_open_order_size(
                OpenOrderSizingInput(
                    symbol=config.symbol,
                    requested_size=requested_size,
                    mark_price=close,
                    equity=account.equity,
                    available_balance=account.available_balance,
                    starting_nav=account.equity,
                    max_leverage=config.max_leverage,
                )
            )
            if sizing.block_reason is not None:
                blocked_signal_count += 1
                continue
            notional = close * sizing.order_size * contract_value
            reserved_margin = notional / max(float(config.max_leverage), 1.0)
            entry_fee = notional * fee_rate
            account = _AccountState(
                equity=account.equity - entry_fee,
                available_balance=account.available_balance - reserved_margin - entry_fee,
            )
            equity_points.append(account.equity)
            position = _Position(
                entry_index=index,
                entry_close=close,
                highest_close=close,
                size=sizing.order_size,
                reserved_margin=reserved_margin,
                entry_fee=entry_fee,
            )

    if position is not None:
        final_close = closes[-1]
        trade_return, account = _close_position(
            position=position,
            close=final_close,
            account=account,
            contract_value=contract_value,
            fee_rate=fee_rate,
            initial_equity=config.initial_equity,
        )
        trades.append(trade_return)
        equity_points.append(account.equity)

    return _build_result(
        parameters=parameters,
        sample_count=len(rows),
        trade_returns=trades,
        equity_points=equity_points,
        initial_equity=config.initial_equity,
        final_equity=account.equity,
        blocked_signal_count=blocked_signal_count,
    )


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
        per_symbol_max_position=config.per_symbol_max_position,
        max_leverage=config.max_leverage,
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


def _close_position(
    *,
    position: _Position,
    close: float,
    account: _AccountState,
    contract_value: float,
    fee_rate: float,
    initial_equity: float,
) -> tuple[float, _AccountState]:
    pnl = (close - position.entry_close) * position.size * contract_value
    exit_fee = close * position.size * contract_value * fee_rate
    net_pnl = pnl - position.entry_fee - exit_fee
    account = _AccountState(
        equity=account.equity + pnl - exit_fee,
        available_balance=account.available_balance + position.reserved_margin + pnl - exit_fee,
    )
    return net_pnl / initial_equity, account


def _build_result(
    *,
    parameters: VolBreakoutParameters,
    sample_count: int,
    trade_returns: list[float],
    equity_points: list[float],
    initial_equity: float,
    final_equity: float,
    blocked_signal_count: int,
) -> VolBreakoutResult:
    trade_count = len(trade_returns)
    return_percent = round(((final_equity / initial_equity) - 1.0) * 100, 6)
    max_drawdown_percent = round(_max_drawdown(equity_points) * 100, 6)
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
        initial_equity=round(initial_equity, 6),
        final_equity=round(final_equity, 6),
        blocked_signal_count=blocked_signal_count,
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
        blocked_signal_count=0,
    )


def _max_drawdown(equity_points: list[float]) -> float:
    if not equity_points:
        return 0.0
    peak = equity_points[0]
    max_drawdown = 0.0
    for equity in equity_points:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
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

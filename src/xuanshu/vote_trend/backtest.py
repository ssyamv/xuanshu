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
class VoteTrendParameters:
    fast_ema_period: int = 20
    slow_ema_period: int = 200
    lookback_bars: int = 6
    channel_bars: int = 24
    threshold_bps: int = 0
    required_votes: int = 4
    stop_loss_bps: int = 75
    take_profit_bps: int = 2400
    max_hold_bars: int = 36
    allow_short: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "fast_ema_period",
            "slow_ema_period",
            "lookback_bars",
            "channel_bars",
            "required_votes",
            "stop_loss_bps",
            "take_profit_bps",
            "max_hold_bars",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.fast_ema_period >= self.slow_ema_period:
            raise ValueError("fast_ema_period must be smaller than slow_ema_period")
        if self.threshold_bps < 0:
            raise ValueError("threshold_bps must be non-negative")
        if self.required_votes > 5:
            raise ValueError("required_votes must be <= 5")


@dataclass(frozen=True, slots=True)
class VoteTrendConfig:
    symbol: str = "BTC-USDT-SWAP"
    initial_equity: float = 1_000.0
    initial_available_balance: float | None = None
    fee_bps: float = 4.0
    risk_fraction: float = 0.25
    per_symbol_max_position: float = 0.12
    max_leverage: int = 3

    def __post_init__(self) -> None:
        if self.initial_equity <= 0 or not math.isfinite(self.initial_equity):
            raise ValueError("initial_equity must be finite and positive")
        if self.initial_available_balance is not None and (
            self.initial_available_balance < 0 or not math.isfinite(self.initial_available_balance)
        ):
            raise ValueError("initial_available_balance must be finite and non-negative")
        if self.fee_bps < 0 or not math.isfinite(self.fee_bps):
            raise ValueError("fee_bps must be finite and non-negative")
        if self.risk_fraction <= 0 or self.risk_fraction > 1:
            raise ValueError("risk_fraction must be > 0 and <= 1")
        if self.per_symbol_max_position < 0 or self.per_symbol_max_position > 1:
            raise ValueError("per_symbol_max_position must be >= 0 and <= 1")
        if self.max_leverage < 1:
            raise ValueError("max_leverage must be positive")


@dataclass(frozen=True, slots=True)
class VoteTrendResult:
    parameters: VoteTrendParameters
    sample_count: int
    trade_count: int
    long_trade_count: int
    short_trade_count: int
    return_percent: float
    max_drawdown_percent: float
    win_rate: float
    profit_factor: float
    initial_equity: float
    final_equity: float
    blocked_signal_count: int


def latest_vote_trend_side(parameters: VoteTrendParameters, historical_rows: list[dict[str, object]]) -> str | None:
    rows = _normalize_rows(historical_rows)
    warmup = max(parameters.slow_ema_period, parameters.lookback_bars, parameters.channel_bars, 14)
    if len(rows) <= warmup:
        return None
    closes = [row["close"] for row in rows]
    highs = [row["high"] for row in rows]
    lows = [row["low"] for row in rows]
    return _signal_side(
        index=len(rows) - 1,
        parameters=parameters,
        closes=closes,
        highs=highs,
        lows=lows,
        fast_ema=_ema(closes, parameters.fast_ema_period),
        slow_ema=_ema(closes, parameters.slow_ema_period),
        rsi_values=_rsi(closes, 14),
    )


def build_vote_trend_snapshot(
    *,
    selected: VoteTrendResult,
    symbol: str,
    bar: str,
    generated_at: datetime,
    config: VoteTrendConfig,
    market_mode: RunMode = RunMode.HALTED,
) -> StrategyConfigSnapshot:
    normalized_time = _normalize_timestamp(generated_at)
    strategy_def_id = _build_strategy_def_id(symbol=symbol, bar=bar, parameters=selected.parameters)
    backtest_report_id = _build_backtest_report_id(symbol=symbol, bar=bar, selected=selected)
    package_id = f"fixed-{strategy_def_id}"
    return StrategyConfigSnapshot(
        version_id=package_id,
        generated_at=normalized_time,
        effective_from=normalized_time,
        expires_at=normalized_time + timedelta(days=3650),
        symbol_whitelist=[symbol],
        strategy_enable_flags={"vote_trend": True},
        risk_multiplier=config.risk_fraction,
        per_symbol_max_position=config.per_symbol_max_position,
        max_leverage=config.max_leverage,
        market_mode=market_mode,
        approval_state=ApprovalState.APPROVED,
        source_reason=f"fixed {symbol} {bar.upper()} vote-trend backtest",
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


@dataclass(frozen=True, slots=True)
class _Position:
    side: str
    entry_index: int
    entry_close: float
    size: float
    reserved_margin: float
    entry_fee: float


@dataclass(frozen=True, slots=True)
class _AccountState:
    equity: float
    available_balance: float


def evaluate_vote_trend(
    parameters: VoteTrendParameters,
    historical_rows: list[dict[str, object]],
    *,
    config: VoteTrendConfig,
) -> VoteTrendResult:
    rows = _normalize_rows(historical_rows)
    warmup = max(parameters.slow_ema_period, parameters.lookback_bars, parameters.channel_bars, 14)
    if len(rows) <= warmup:
        return _empty_result(parameters=parameters, sample_count=len(rows), initial_equity=config.initial_equity)

    contract_value = CONTRACT_VALUE_BY_SYMBOL.get(config.symbol)
    if contract_value is None:
        raise ValueError(f"unsupported contract symbol for vote_trend backtest: {config.symbol}")
    closes = [row["close"] for row in rows]
    highs = [row["high"] for row in rows]
    lows = [row["low"] for row in rows]
    fast_ema = _ema(closes, parameters.fast_ema_period)
    slow_ema = _ema(closes, parameters.slow_ema_period)
    rsi_values = _rsi(closes, 14)
    fee_rate = config.fee_bps / 10_000
    initial_available_balance = (
        config.initial_equity if config.initial_available_balance is None else config.initial_available_balance
    )
    account = _AccountState(config.initial_equity, initial_available_balance)
    equity_points = [account.equity]
    trade_returns: list[float] = []
    long_trade_count = 0
    short_trade_count = 0
    blocked_signal_count = 0
    position: _Position | None = None

    for index in range(warmup, len(rows)):
        close = closes[index]
        signal_side = _signal_side(
            index=index,
            parameters=parameters,
            closes=closes,
            highs=highs,
            lows=lows,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
            rsi_values=rsi_values,
        )
        if position is not None:
            active_return = _active_return(position=position, close=close)
            is_opposite = (
                signal_side is not None
                and ((position.side == "long" and signal_side == "short") or (position.side == "short" and signal_side == "long"))
            )
            if (
                active_return <= -(parameters.stop_loss_bps / 10_000)
                or active_return >= parameters.take_profit_bps / 10_000
                or index - position.entry_index >= parameters.max_hold_bars
                or is_opposite
            ):
                trade_return, account = _close_position(
                    position=position,
                    close=close,
                    account=account,
                    contract_value=contract_value,
                    fee_rate=fee_rate,
                    initial_equity=config.initial_equity,
                )
                trade_returns.append(trade_return)
                if position.side == "long":
                    long_trade_count += 1
                else:
                    short_trade_count += 1
                equity_points.append(account.equity)
                position = None
                continue
            equity_points.append(account.equity + _pnl(position=position, close=close, contract_value=contract_value))
            continue

        if signal_side is None or index >= len(rows) - 1:
            continue
        sizing = calculate_open_order_size(
            OpenOrderSizingInput(
                symbol=config.symbol,
                requested_size=_requested_size(account=account, config=config),
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
            side=signal_side,
            entry_index=index,
            entry_close=close,
            size=sizing.order_size,
            reserved_margin=reserved_margin,
            entry_fee=entry_fee,
        )

    if position is not None:
        trade_return, account = _close_position(
            position=position,
            close=closes[-1],
            account=account,
            contract_value=contract_value,
            fee_rate=fee_rate,
            initial_equity=config.initial_equity,
        )
        trade_returns.append(trade_return)
        if position.side == "long":
            long_trade_count += 1
        else:
            short_trade_count += 1
        equity_points.append(account.equity)

    return _build_result(
        parameters=parameters,
        sample_count=len(rows),
        trade_returns=trade_returns,
        equity_points=equity_points,
        long_trade_count=long_trade_count,
        short_trade_count=short_trade_count,
        initial_equity=config.initial_equity,
        final_equity=account.equity,
        blocked_signal_count=blocked_signal_count,
    )


def _signal_side(
    *,
    index: int,
    parameters: VoteTrendParameters,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    fast_ema: list[float],
    slow_ema: list[float],
    rsi_values: list[float],
) -> str | None:
    momentum = (closes[index] / closes[index - parameters.lookback_bars]) - 1.0
    channel_high = max(highs[index - parameters.channel_bars : index])
    channel_low = min(lows[index - parameters.channel_bars : index])
    threshold = parameters.threshold_bps / 10_000
    long_votes = sum(
        [
            closes[index] > slow_ema[index],
            fast_ema[index] > slow_ema[index],
            momentum > threshold,
            closes[index] > channel_high,
            rsi_values[index] > 50,
        ]
    )
    short_votes = sum(
        [
            closes[index] < slow_ema[index],
            fast_ema[index] < slow_ema[index],
            momentum < -threshold,
            closes[index] < channel_low,
            rsi_values[index] < 50,
        ]
    )
    if long_votes >= parameters.required_votes:
        return "long"
    if parameters.allow_short and short_votes >= parameters.required_votes:
        return "short"
    return None


def _requested_size(*, account: _AccountState, config: VoteTrendConfig) -> float:
    return max(
        1.0,
        min(
            account.equity * config.per_symbol_max_position * config.risk_fraction,
            account.equity * 0.0035,
        ),
    )


def _active_return(*, position: _Position, close: float) -> float:
    if position.side == "short":
        return (position.entry_close - close) / position.entry_close
    return (close / position.entry_close) - 1.0


def _pnl(*, position: _Position, close: float, contract_value: float) -> float:
    if position.side == "short":
        return (position.entry_close - close) * position.size * contract_value
    return (close - position.entry_close) * position.size * contract_value


def _close_position(
    *,
    position: _Position,
    close: float,
    account: _AccountState,
    contract_value: float,
    fee_rate: float,
    initial_equity: float,
) -> tuple[float, _AccountState]:
    pnl = _pnl(position=position, close=close, contract_value=contract_value)
    exit_fee = close * position.size * contract_value * fee_rate
    net_pnl = pnl - position.entry_fee - exit_fee
    account = _AccountState(
        equity=account.equity + pnl - exit_fee,
        available_balance=account.available_balance + position.reserved_margin + pnl - exit_fee,
    )
    return net_pnl / initial_equity, account


def _build_result(
    *,
    parameters: VoteTrendParameters,
    sample_count: int,
    trade_returns: list[float],
    equity_points: list[float],
    long_trade_count: int,
    short_trade_count: int,
    initial_equity: float,
    final_equity: float,
    blocked_signal_count: int,
) -> VoteTrendResult:
    trade_count = len(trade_returns)
    wins = [value for value in trade_returns if value > 0]
    losses = [value for value in trade_returns if value < 0]
    return VoteTrendResult(
        parameters=parameters,
        sample_count=sample_count,
        trade_count=trade_count,
        long_trade_count=long_trade_count,
        short_trade_count=short_trade_count,
        return_percent=round(((final_equity / initial_equity) - 1.0) * 100, 6),
        max_drawdown_percent=round(_max_drawdown(equity_points) * 100, 6),
        win_rate=round(len(wins) / trade_count, 6) if trade_count else 0.0,
        profit_factor=_profit_factor(wins=wins, losses=losses),
        initial_equity=round(initial_equity, 6),
        final_equity=round(final_equity, 6),
        blocked_signal_count=blocked_signal_count,
    )


def _empty_result(*, parameters: VoteTrendParameters, sample_count: int, initial_equity: float) -> VoteTrendResult:
    return VoteTrendResult(
        parameters=parameters,
        sample_count=sample_count,
        trade_count=0,
        long_trade_count=0,
        short_trade_count=0,
        return_percent=0.0,
        max_drawdown_percent=0.0,
        win_rate=0.0,
        profit_factor=0.0,
        initial_equity=round(initial_equity, 6),
        final_equity=round(initial_equity, 6),
        blocked_signal_count=0,
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


def _normalize_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _extract_positive_float(row: dict[str, object], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"historical rows must include numeric {key}")
    parsed = float(value)
    if parsed <= 0 or not math.isfinite(parsed):
        raise ValueError(f"historical {key} must be finite and positive")
    return parsed


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


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


def _build_strategy_def_id(*, symbol: str, bar: str, parameters: VoteTrendParameters) -> str:
    mode = "both" if parameters.allow_short else "longonly"
    return (
        f"vote-trend-{symbol.lower()}-{bar.lower()}"
        f"-f{parameters.fast_ema_period}-s{parameters.slow_ema_period}"
        f"-lb{parameters.lookback_bars}-ch{parameters.channel_bars}"
        f"-th{parameters.threshold_bps}-v{parameters.required_votes}"
        f"-sl{parameters.stop_loss_bps}-tp{parameters.take_profit_bps}"
        f"-h{parameters.max_hold_bars}-{mode}"
    )


def _build_backtest_report_id(*, symbol: str, bar: str, selected: VoteTrendResult) -> str:
    digest = sha256(repr(selected).encode("utf-8")).hexdigest()[:12]
    return f"bt-vote-trend-{symbol.lower()}-{bar.lower()}-{digest}"

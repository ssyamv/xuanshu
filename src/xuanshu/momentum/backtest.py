from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import math
from numbers import Real

from xuanshu.contracts.strategy import ApprovedStrategyBinding, StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode


@dataclass(frozen=True, slots=True)
class MomentumParameterSet:
    lookback: int
    stop_loss_bps: int
    take_profit_bps: int
    max_hold_minutes: int

    def __post_init__(self) -> None:
        if self.lookback <= 0:
            raise ValueError("lookback must be positive")
        if self.stop_loss_bps <= 0:
            raise ValueError("stop_loss_bps must be positive")
        if self.take_profit_bps <= 0:
            raise ValueError("take_profit_bps must be positive")
        if self.max_hold_minutes <= 0:
            raise ValueError("max_hold_minutes must be positive")


@dataclass(frozen=True, slots=True)
class MomentumBacktestConfig:
    min_trade_count: int = 30
    max_drawdown_percent: float = 20.0
    risk_fraction: float = 0.25

    def __post_init__(self) -> None:
        if self.min_trade_count <= 0:
            raise ValueError("min_trade_count must be positive")
        if self.max_drawdown_percent <= 0 or not math.isfinite(self.max_drawdown_percent):
            raise ValueError("max_drawdown_percent must be finite and positive")
        if self.risk_fraction <= 0 or self.risk_fraction > 1 or not math.isfinite(self.risk_fraction):
            raise ValueError("risk_fraction must be > 0 and <= 1")


@dataclass(frozen=True, slots=True)
class MomentumBacktestResult:
    parameters: MomentumParameterSet
    sample_count: int
    trade_count: int
    return_percent: float
    max_drawdown_percent: float
    win_rate: float
    profit_factor: float
    stability_score: float


@dataclass(frozen=True, slots=True)
class _Position:
    entry_time: datetime
    entry_close: float


def evaluate_momentum_candidate(
    parameters: MomentumParameterSet,
    historical_rows: list[dict[str, object]],
    *,
    risk_fraction: float = 1.0,
) -> MomentumBacktestResult:
    rows = _normalize_rows(historical_rows)
    if parameters.lookback >= len(rows):
        return _empty_result(parameters=parameters, sample_count=len(rows))

    trades: list[float] = []
    position: _Position | None = None
    max_hold = timedelta(minutes=parameters.max_hold_minutes)

    for index in range(parameters.lookback, len(rows)):
        timestamp, close = rows[index]
        if position is not None:
            exit_return = _exit_return(
                position=position,
                timestamp=timestamp,
                close=close,
                stop_loss_bps=parameters.stop_loss_bps,
                take_profit_bps=parameters.take_profit_bps,
                max_hold=max_hold,
            )
            if exit_return is not None:
                trades.append(exit_return * risk_fraction)
                position = None
                continue

        if position is None and index < len(rows) - 1:
            lookback_close = rows[index - parameters.lookback][1]
            if close > lookback_close:
                position = _Position(entry_time=timestamp, entry_close=close)

    if position is not None:
        _, final_close = rows[-1]
        trades.append(((final_close / position.entry_close) - 1.0) * risk_fraction)

    return _build_result(parameters=parameters, sample_count=len(rows), trade_returns=trades)


def select_best_candidate(
    results: list[MomentumBacktestResult],
    *,
    config: MomentumBacktestConfig,
) -> MomentumBacktestResult | None:
    passing = [
        result
        for result in results
        if result.trade_count >= config.min_trade_count
        and result.return_percent > 0
        and result.profit_factor > 1
        and result.max_drawdown_percent <= config.max_drawdown_percent
    ]
    if not passing:
        return None
    return max(passing, key=lambda result: (result.stability_score, result.return_percent, result.profit_factor))


def build_momentum_snapshot(
    *,
    selected: MomentumBacktestResult,
    symbol: str,
    generated_at: datetime,
    config: MomentumBacktestConfig,
) -> StrategyConfigSnapshot:
    normalized_time = _normalize_timestamp(generated_at)
    strategy_def_id = _build_strategy_def_id(symbol=symbol, parameters=selected.parameters)
    backtest_report_id = _build_backtest_report_id(symbol=symbol, selected=selected)
    package_id = f"pkg-{strategy_def_id}"
    return StrategyConfigSnapshot(
        version_id=f"fixed-{strategy_def_id}",
        generated_at=normalized_time,
        effective_from=normalized_time,
        expires_at=normalized_time + timedelta(days=3650),
        symbol_whitelist=[symbol],
        strategy_enable_flags={"momentum": True},
        risk_multiplier=config.risk_fraction,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.HALTED,
        approval_state=ApprovalState.APPROVED,
        source_reason="fixed momentum backtest",
        ttl_sec=315_360_000,
        symbol_strategy_bindings={
            symbol: ApprovedStrategyBinding(
                strategy_def_id=strategy_def_id,
                strategy_package_id=package_id,
                backtest_report_id=backtest_report_id,
                score=max(0.0, selected.return_percent),
                score_basis="backtest_return_percent",
                approval_record_id=f"apr-{backtest_report_id}",
                activated_at=normalized_time,
            )
        },
    )


def _exit_return(
    *,
    position: _Position,
    timestamp: datetime,
    close: float,
    stop_loss_bps: int,
    take_profit_bps: int,
    max_hold: timedelta,
) -> float | None:
    raw_return = (close / position.entry_close) - 1.0
    if raw_return <= -(stop_loss_bps / 10_000):
        return raw_return
    if raw_return >= take_profit_bps / 10_000:
        return raw_return
    if timestamp - position.entry_time >= max_hold:
        return raw_return
    return None


def _build_result(
    *,
    parameters: MomentumParameterSet,
    sample_count: int,
    trade_returns: list[float],
) -> MomentumBacktestResult:
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
    return MomentumBacktestResult(
        parameters=parameters,
        sample_count=sample_count,
        trade_count=trade_count,
        return_percent=return_percent,
        max_drawdown_percent=max_drawdown_percent,
        win_rate=win_rate,
        profit_factor=profit_factor,
        stability_score=stability_score,
    )


def _empty_result(*, parameters: MomentumParameterSet, sample_count: int) -> MomentumBacktestResult:
    return MomentumBacktestResult(
        parameters=parameters,
        sample_count=sample_count,
        trade_count=0,
        return_percent=0.0,
        max_drawdown_percent=0.0,
        win_rate=0.0,
        profit_factor=0.0,
        stability_score=0.0,
    )


def _normalize_rows(rows: list[dict[str, object]]) -> list[tuple[datetime, float]]:
    normalized = [(_extract_timestamp(row), _extract_close(row)) for row in rows]
    normalized.sort(key=lambda item: item[0])
    for previous, current in zip(normalized, normalized[1:], strict=False):
        if previous[0] == current[0]:
            raise ValueError("historical rows must have unique timestamps")
    return normalized


def _extract_timestamp(row: dict[str, object]) -> datetime:
    timestamp = row.get("timestamp")
    if not isinstance(timestamp, datetime):
        raise ValueError("historical rows must include datetime timestamp")
    return _normalize_timestamp(timestamp)


def _extract_close(row: dict[str, object]) -> float:
    close = row.get("close")
    if isinstance(close, bool) or not isinstance(close, Real):
        raise ValueError("historical rows must include numeric close")
    close_float = float(close)
    if close_float <= 0 or not math.isfinite(close_float):
        raise ValueError("historical close must be finite and positive")
    return close_float


def _normalize_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


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


def _build_strategy_def_id(*, symbol: str, parameters: MomentumParameterSet) -> str:
    normalized_symbol = symbol.lower().replace("-", "-")
    digest = _parameter_digest(symbol=symbol, parameters=parameters)
    return f"momentum-{normalized_symbol}-{digest}"


def _build_backtest_report_id(*, symbol: str, selected: MomentumBacktestResult) -> str:
    digest = _parameter_digest(symbol=symbol, parameters=selected.parameters)
    return f"bt-{digest}"


def _parameter_digest(*, symbol: str, parameters: MomentumParameterSet) -> str:
    payload = (
        f"{symbol}|{parameters.lookback}|{parameters.stop_loss_bps}|"
        f"{parameters.take_profit_bps}|{parameters.max_hold_minutes}"
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:12]

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import math
from datetime import UTC, datetime
from decimal import Decimal
from numbers import Real

from xuanshu.contracts.backtest import (
    BacktestDatasetRange,
    BacktestReport,
    OverfitRisk,
    RegimeFit,
    TradeCountSufficiency,
)
from xuanshu.contracts.research import StrategyPackage


@dataclass(frozen=True, slots=True)
class _Position:
    side: int
    entry_time: datetime
    entry_close: float


class BacktestValidator:
    REPORT_SCHEMA_VERSION = "v1"
    ZERO_LOSS_PROFIT_FACTOR = 999.0

    def validate(
        self,
        *,
        package: StrategyPackage,
        historical_rows: list[dict[str, object]],
    ) -> BacktestReport:
        if len(package.symbol_scope) != 1:
            raise ValueError("BacktestValidator currently supports exactly one symbol")
        if not historical_rows:
            raise ValueError("historical_rows must not be empty")

        definition = package.strategy_definition
        strategy_family = self._normalize_strategy_family(definition.strategy_family)
        allowed_sides = self._normalize_directionality(definition.directionality)
        lookback = self._extract_positive_int(definition.parameter_set, "lookback", default=1)
        entry_signal = self._extract_entry_signal(
            strategy_family=strategy_family,
            parameter_set=definition.parameter_set,
            lookback=lookback,
        )
        stop_loss_bps = self._extract_exit_rule_float(definition.exit_rules, "stop_loss_bps")
        take_profit_bps = self._extract_exit_rule_float(definition.exit_rules, "take_profit_bps")
        risk_fraction = self._extract_positive_float(
            package.position_sizing_rules,
            "risk_fraction",
            default=1.0,
            upper_bound=1.0,
        )
        max_hold_minutes = self._extract_exit_rule_int(
            definition.exit_rules,
            "time_stop_minutes",
        )
        if entry_signal == "range_retest" and lookback < 2:
            raise ValueError("range_retest requires lookback >= 2")
        normalized_rows = self._normalize_rows(historical_rows)
        trades = self._simulate_trades(
            normalized_rows=normalized_rows,
            strategy_family=strategy_family,
            entry_signal=entry_signal,
            allowed_sides=allowed_sides,
            lookback=lookback,
            stop_loss_bps=stop_loss_bps,
            take_profit_bps=take_profit_bps,
            risk_fraction=risk_fraction,
            max_hold_minutes=max_hold_minutes,
        )
        trade_pnls = [trade["pnl"] for trade in trades]
        net_pnl = sum(trade_pnls)
        max_drawdown = self._compute_max_drawdown(trade_pnls)
        trade_count = len(trades)
        trade_count_sufficiency = self._assess_trade_count_sufficiency(
            sample_count=len(historical_rows),
            trade_count=trade_count,
        )
        win_rate = self._compute_win_rate(trade_pnls)
        profit_factor = self._compute_profit_factor(trade_pnls)
        stability_score = self._compute_stability_score(
            trade_pnls=trade_pnls,
            max_drawdown=max_drawdown,
            trade_count_sufficiency=trade_count_sufficiency,
        )
        overfit_risk = self._assess_overfit_risk(
            trade_count_sufficiency=trade_count_sufficiency,
            stability_score=stability_score,
            trade_pnls=trade_pnls,
            trade_count=trade_count,
        )
        regime_fit = self._assess_regime_fit(
            package=package,
            strategy_family=strategy_family,
            trades=trades,
            net_pnl=net_pnl,
        )
        timestamps = [timestamp for timestamp, _ in normalized_rows]

        return BacktestReport(
            backtest_report_id=self._build_report_id(
                strategy_package_id=package.strategy_package_id,
                normalized_rows=normalized_rows,
            ),
            strategy_package_id=package.strategy_package_id,
            strategy_def_id=definition.strategy_def_id,
            symbol_scope=package.symbol_scope,
            dataset_range=BacktestDatasetRange(
                start=timestamps[0].astimezone(UTC),
                end=timestamps[-1].astimezone(UTC),
                regime_fit=RegimeFit(regime_fit),
            ),
            sample_count=len(historical_rows),
            trade_count=trade_count,
            trade_count_sufficiency=trade_count_sufficiency,
            net_pnl=net_pnl,
            return_percent=round(net_pnl * 100, 6),
            max_drawdown=max_drawdown,
            win_rate=win_rate,
            profit_factor=profit_factor,
            stability_score=stability_score,
            overfit_risk=overfit_risk,
            failure_modes=package.failure_modes,
            invalidating_conditions=package.invalidating_conditions,
            generated_at=timestamps[-1].astimezone(UTC),
        )

    @classmethod
    def _simulate_trades(
        cls,
        *,
        normalized_rows: list[tuple[datetime, float]],
        strategy_family: str,
        entry_signal: str,
        allowed_sides: set[int],
        lookback: int,
        stop_loss_bps: float,
        take_profit_bps: float,
        risk_fraction: float,
        max_hold_minutes: int,
    ) -> list[dict[str, float | str]]:
        if lookback >= len(normalized_rows):
            return []

        trades: list[dict[str, float | str]] = []
        position: _Position | None = None

        for index in range(lookback, len(normalized_rows)):
            timestamp, close = normalized_rows[index]
            exited_this_bar = False
            if position is not None:
                exit_reason = cls._evaluate_exit(
                    position=position,
                    current_time=timestamp,
                    current_close=close,
                    stop_loss_bps=stop_loss_bps,
                    take_profit_bps=take_profit_bps,
                    max_hold_minutes=max_hold_minutes,
                )
                if exit_reason is not None:
                    trades.append(
                        cls._build_trade(
                            position=position,
                            exit_time=timestamp,
                            exit_close=close,
                            risk_fraction=risk_fraction,
                            exit_reason=exit_reason,
                        )
                    )
                    position = None
                    exited_this_bar = True

            if position is not None or exited_this_bar or index == len(normalized_rows) - 1:
                continue

            signal_side = cls._compute_signal_side(
                strategy_family=strategy_family,
                entry_signal=entry_signal,
                current_close=close,
                previous_close=normalized_rows[index - 1][1],
                lookback_close=normalized_rows[index - lookback][1],
            )
            if signal_side is None or signal_side not in allowed_sides:
                continue

            position = _Position(
                side=signal_side,
                entry_time=timestamp,
                entry_close=close,
            )

        if position is not None:
            exit_time, exit_close = normalized_rows[-1]
            trades.append(
                cls._build_trade(
                    position=position,
                    exit_time=exit_time,
                    exit_close=exit_close,
                    risk_fraction=risk_fraction,
                    exit_reason="end_of_data",
                )
            )
        return trades

    @staticmethod
    def _extract_close(row: dict[str, object]) -> float:
        if "close" not in row:
            raise ValueError("historical_rows must include a close value")
        close_value = row["close"]
        if isinstance(close_value, bool) or not isinstance(close_value, Real | Decimal):
            raise ValueError("historical_rows close values must be real numbers")
        try:
            normalized_close = float(close_value)
        except OverflowError as exc:
            raise ValueError("historical_rows close values must be finite") from exc
        if not math.isfinite(normalized_close):
            raise ValueError("historical_rows close values must be finite")
        if normalized_close <= 0.0:
            raise ValueError("historical_rows close values must be > 0")
        return normalized_close

    @classmethod
    def _normalize_rows(cls, historical_rows: list[dict[str, object]]) -> list[tuple[datetime, float]]:
        normalized_rows = [
            (cls._extract_timestamp(row).astimezone(UTC), cls._extract_close(row))
            for row in historical_rows
        ]
        normalized_rows.sort(key=lambda item: item[0])
        for previous, current in zip(normalized_rows, normalized_rows[1:], strict=False):
            if previous[0] == current[0]:
                raise ValueError("historical_rows timestamps must be unique")
        return normalized_rows

    @staticmethod
    def _normalize_strategy_family(strategy_family: str) -> str:
        normalized = strategy_family.strip().lower()
        if normalized not in {"breakout", "mean_reversion"}:
            raise ValueError("unsupported strategy_family")
        return normalized

    @staticmethod
    def _normalize_directionality(directionality: str) -> set[int]:
        normalized = directionality.strip().lower()
        if normalized in {"long_only", "long"}:
            return {1}
        if normalized in {"short_only", "short"}:
            return {-1}
        if normalized in {"long_short", "both"}:
            return {1, -1}
        raise ValueError("unsupported directionality")

    @staticmethod
    def _extract_entry_signal(
        *,
        strategy_family: str,
        parameter_set: dict[str, object],
        lookback: int,
    ) -> str:
        signal = parameter_set.get("signal_mode")
        supported_signals = {
            "breakout": {"breakout_confirmed"},
            "mean_reversion": {"mean_reversion_signal", "range_retest"},
        }
        if isinstance(signal, str) and signal.strip():
            normalized_signal = signal.strip().lower()
            if normalized_signal not in supported_signals[strategy_family]:
                raise ValueError("signal_mode is not supported for strategy_family")
            return normalized_signal
        if strategy_family == "breakout":
            return "breakout_confirmed"
        return "range_retest" if lookback >= 2 else "mean_reversion_signal"

    @classmethod
    def _extract_exit_rule_float(
        cls,
        exit_rules: object,
        key: str,
    ) -> float:
        value = cls._find_exit_rule_value(exit_rules, key)
        if value is None:
            raise ValueError(f"{key} must be present in exit_rules")
        if isinstance(value, bool) or not isinstance(value, Real | Decimal):
            raise ValueError(f"{key} must be numeric")
        try:
            normalized_value = float(value)
        except OverflowError as exc:
            raise ValueError(f"{key} must be finite") from exc
        if not math.isfinite(normalized_value) or normalized_value <= 0.0:
            raise ValueError(f"{key} must be > 0")
        return normalized_value

    @classmethod
    def _extract_exit_rule_int(
        cls,
        exit_rules: object,
        key: str,
    ) -> int:
        value = cls._find_exit_rule_value(exit_rules, key)
        if value is None:
            raise ValueError(f"{key} must be present in exit_rules")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
        return value

    @classmethod
    def _find_exit_rule_value(cls, node: object, key: str) -> object | None:
        if isinstance(node, Mapping):
            op = node.get("op")
            if isinstance(op, str) and op.strip().lower() == key:
                return node.get("value")
            for combinator in ("all", "any"):
                children = node.get(combinator)
                if isinstance(children, list):
                    for child in children:
                        value = cls._find_exit_rule_value(child, key)
                        if value is not None:
                            return value
        elif isinstance(node, list):
            for child in node:
                value = cls._find_exit_rule_value(child, key)
                if value is not None:
                    return value
        return None

    @staticmethod
    def _extract_positive_int(config: dict[str, object], key: str, *, default: int) -> int:
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        if value < 1:
            raise ValueError(f"{key} must be >= 1")
        return value

    @staticmethod
    def _extract_positive_float(
        config: dict[str, object],
        key: str,
        *,
        default: float,
        upper_bound: float | None = None,
    ) -> float:
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, Real | Decimal):
            raise ValueError(f"{key} must be numeric")
        try:
            normalized_value = float(value)
        except OverflowError as exc:
            raise ValueError(f"{key} must be finite") from exc
        if not math.isfinite(normalized_value) or normalized_value <= 0.0:
            raise ValueError(f"{key} must be > 0")
        if upper_bound is not None and normalized_value > upper_bound:
            raise ValueError(f"{key} must be <= {upper_bound:g}")
        return normalized_value

    @staticmethod
    def _build_report_id(
        *,
        strategy_package_id: str,
        normalized_rows: list[tuple[datetime, float]],
    ) -> str:
        fingerprint = "|".join(
            f"{timestamp.isoformat()}={close.hex()}"
            for timestamp, close in normalized_rows
        )
        versioned_fingerprint = f"{BacktestValidator.REPORT_SCHEMA_VERSION}|{strategy_package_id}|{fingerprint}"
        digest = sha256(versioned_fingerprint.encode("utf-8")).hexdigest()[:12]
        return f"{strategy_package_id}-report-{digest}"

    @staticmethod
    def _compute_signal_side(
        *,
        strategy_family: str,
        entry_signal: str,
        current_close: float,
        previous_close: float,
        lookback_close: float,
    ) -> int | None:
        if entry_signal == "range_retest":
            if previous_close < lookback_close and current_close >= lookback_close:
                return 1
            if previous_close > lookback_close and current_close <= lookback_close:
                return -1
            return None
        if current_close == lookback_close:
            return None
        upward_move = current_close > lookback_close
        if strategy_family == "breakout":
            return 1 if upward_move else -1
        return -1 if upward_move else 1

    @staticmethod
    def _extract_timestamp(row: dict[str, object]) -> datetime:
        timestamp = row.get("timestamp")
        if not isinstance(timestamp, datetime):
            raise ValueError("historical_rows timestamp values must be datetimes")
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("historical_rows timestamp values must be timezone-aware")
        return timestamp

    @staticmethod
    def _evaluate_exit(
        *,
        position: _Position,
        current_time: datetime,
        current_close: float,
        stop_loss_bps: float,
        take_profit_bps: float,
        max_hold_minutes: int,
    ) -> str | None:
        pnl_bps = position.side * ((current_close - position.entry_close) / position.entry_close) * 10000
        if pnl_bps <= -stop_loss_bps:
            return "stop_loss"
        if pnl_bps >= take_profit_bps:
            return "take_profit"
        held_minutes = (current_time - position.entry_time).total_seconds() / 60
        if held_minutes >= max_hold_minutes:
            return "max_hold"
        return None

    @staticmethod
    def _build_trade(
        *,
        position: _Position,
        exit_time: datetime,
        exit_close: float,
        risk_fraction: float,
        exit_reason: str,
    ) -> dict[str, float | str]:
        pnl = position.side * ((exit_close - position.entry_close) / position.entry_close) * risk_fraction
        return {
            "pnl": pnl,
            "exit_reason": exit_reason,
            "held_minutes": (exit_time - position.entry_time).total_seconds() / 60,
        }

    @staticmethod
    def _compute_max_drawdown(trade_pnls: list[float]) -> float:
        if not trade_pnls:
            return 0.0
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for trade_pnl in trade_pnls:
            equity += trade_pnl
            if equity > peak:
                peak = equity
            drawdown = peak - equity
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        return max_drawdown

    @staticmethod
    def _assess_trade_count_sufficiency(
        *,
        sample_count: int,
        trade_count: int,
    ) -> TradeCountSufficiency:
        if sample_count >= 3 and trade_count >= 2:
            return TradeCountSufficiency.SUFFICIENT
        return TradeCountSufficiency.INSUFFICIENT

    @staticmethod
    def _compute_win_rate(trade_pnls: list[float]) -> float:
        if not trade_pnls:
            return 0.0
        winning_trades = sum(1 for trade_pnl in trade_pnls if trade_pnl > 0)
        return winning_trades / len(trade_pnls)

    @staticmethod
    def _compute_profit_factor(trade_pnls: list[float]) -> float:
        gross_profit = sum(trade_pnl for trade_pnl in trade_pnls if trade_pnl > 0)
        gross_loss = -sum(trade_pnl for trade_pnl in trade_pnls if trade_pnl < 0)
        if gross_loss > 0:
            return gross_profit / gross_loss
        if gross_profit > 0:
            return BacktestValidator.ZERO_LOSS_PROFIT_FACTOR
        return 0.0

    @staticmethod
    def _compute_stability_score(
        *,
        trade_pnls: list[float],
        max_drawdown: float,
        trade_count_sufficiency: TradeCountSufficiency,
    ) -> float:
        if not trade_pnls:
            return 0.0

        adequacy_score = 1.0 if trade_count_sufficiency is TradeCountSufficiency.SUFFICIENT else 0.5
        win_rate_score = sum(1 for trade_pnl in trade_pnls if trade_pnl > 0) / len(trade_pnls)
        mean_abs_trade_pnl = sum(abs(trade_pnl) for trade_pnl in trade_pnls) / len(trade_pnls)
        if mean_abs_trade_pnl == 0.0:
            variability_score = 0.0
        else:
            mean_trade_pnl = sum(trade_pnls) / len(trade_pnls)
            dispersion = sum(abs(trade_pnl - mean_trade_pnl) for trade_pnl in trade_pnls) / len(trade_pnls)
            variability_score = max(0.0, 1.0 - min(dispersion / (mean_abs_trade_pnl * 2), 1.0))

        if max_drawdown == 0.0:
            drawdown_score = 1.0
        else:
            gross_profit = sum(trade_pnl for trade_pnl in trade_pnls if trade_pnl > 0)
            base = gross_profit if gross_profit > 0 else max_drawdown
            drawdown_score = max(0.0, 1.0 - min(max_drawdown / base, 1.0))

        return round((adequacy_score + win_rate_score + variability_score + drawdown_score) / 4, 6)

    @staticmethod
    def _assess_overfit_risk(
        *,
        trade_count_sufficiency: TradeCountSufficiency,
        stability_score: float,
        trade_pnls: list[float],
        trade_count: int,
    ) -> OverfitRisk:
        if trade_count_sufficiency is TradeCountSufficiency.INSUFFICIENT:
            return OverfitRisk.HIGH

        max_drawdown = BacktestValidator._compute_max_drawdown(trade_pnls)
        gross_profit = sum(trade_pnl for trade_pnl in trade_pnls if trade_pnl > 0)
        normalized_drawdown = 0.0 if max_drawdown == 0.0 else max_drawdown / max(gross_profit, max_drawdown)
        if trade_count >= 3 and stability_score >= 0.7 and normalized_drawdown <= 0.5:
            return OverfitRisk.LOW
        return OverfitRisk.MEDIUM

    @staticmethod
    def _assess_regime_fit(
        *,
        package: StrategyPackage,
        strategy_family: str,
        trades: list[dict[str, float | str]],
        net_pnl: float,
    ) -> str:
        if not package.market_environment_scope or not trades:
            return "unknown"

        normalized_scope = {regime.strip().lower() for regime in package.market_environment_scope}
        expected_regime = "trend" if strategy_family == "breakout" else "range"
        if expected_regime not in normalized_scope:
            return "unknown"
        return "aligned" if net_pnl > 0 else "misaligned"

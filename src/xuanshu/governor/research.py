from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
import json

from xuanshu.contracts.research import ResearchTrigger, StrategyPackage


class StrategyResearchEngine:
    def build_candidate_package(
        self,
        *,
        trigger: ResearchTrigger,
        symbol_scope: list[str],
        market_environment: str,
        historical_rows: list[dict[str, object]],
        research_reason: str,
    ) -> StrategyPackage:
        normalized_market_environment = self._normalize_text(market_environment, "market_environment")
        normalized_research_reason = self._normalize_text(research_reason, "research_reason")
        normalized_symbol_scope = [self._normalize_text(symbol, "symbol_scope") for symbol in symbol_scope]
        if not normalized_market_environment:
            raise ValueError("market_environment must not be blank")
        if not normalized_research_reason:
            raise ValueError("research_reason must not be blank")
        if not normalized_symbol_scope:
            raise ValueError("symbol_scope must not be blank")
        if not historical_rows:
            raise ValueError("historical_rows must not be empty")

        normalized_rows = self._normalize_historical_rows(historical_rows)
        closes = [row["close"] for row in normalized_rows]
        start_close = closes[0]
        end_close = closes[-1]
        change_ratio = self._safe_ratio(end_close - start_close, start_close)
        backtest_summary = {
            "row_count": len(historical_rows),
            "start_close": start_close,
            "end_close": end_close,
            "close_change_bps": round(change_ratio * 10000, 6),
        }
        performance_summary = {
            "return_percent": round(change_ratio * 100, 6),
        }
        parameter_set = {
            "row_count": len(historical_rows),
            "start_close": start_close,
            "end_close": end_close,
        }
        strategy_package_id = self._build_package_id(
            trigger=trigger,
            symbol_scope=normalized_symbol_scope,
            market_environment=normalized_market_environment,
            historical_rows=normalized_rows,
            research_reason=normalized_research_reason,
        )

        return StrategyPackage(
            strategy_package_id=strategy_package_id,
            generated_at=self._extract_generated_at(normalized_rows),
            trigger=trigger,
            symbol_scope=normalized_symbol_scope,
            market_environment_scope=[normalized_market_environment],
            strategy_family=self._select_strategy_family(normalized_market_environment),
            directionality="long_short",
            entry_rules={
                "signal": self._select_entry_signal(normalized_market_environment),
            },
            exit_rules={
                "stop_loss_bps": 50,
                "take_profit_bps": 120,
            },
            position_sizing_rules={
                "risk_fraction": 0.0025,
            },
            risk_constraints={
                "max_hold_minutes": 60,
            },
            parameter_set=parameter_set,
            backtest_summary=backtest_summary,
            performance_summary=performance_summary,
            failure_modes=[],
            invalidating_conditions=[],
            research_reason=normalized_research_reason,
        )

    @staticmethod
    def _extract_close(row: Mapping[str, object]) -> float:
        close_value = row.get("close")
        if close_value is None:
            raise ValueError("historical_rows must include a close value")
        return float(close_value)

    @staticmethod
    def _extract_generated_at(historical_rows: list[dict[str, object]]) -> datetime:
        timestamps: list[datetime] = []
        for row in historical_rows:
            timestamp = row.get("timestamp")
            if not isinstance(timestamp, datetime):
                raise ValueError("historical_rows timestamp values must be datetimes")
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("historical_rows timestamp values must be timezone-aware")
            timestamps.append(timestamp.astimezone(UTC))
        latest_timestamp = max(timestamps)
        return latest_timestamp.astimezone(UTC)

    @staticmethod
    def _build_package_id(
        *,
        trigger: ResearchTrigger,
        symbol_scope: list[str],
        market_environment: str,
        historical_rows: list[dict[str, object]],
        research_reason: str,
    ) -> str:
        fingerprint = {
            "trigger": trigger.value,
            "symbol_scope": symbol_scope,
            "market_environment": market_environment,
            "historical_rows": StrategyResearchEngine._canonicalize_historical_rows(historical_rows),
            "research_reason": research_reason,
        }
        digest = sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return f"pkg-{digest[:12]}"

    @staticmethod
    def _safe_ratio(numerator: float, denominator: float) -> float:
        if denominator == 0:
            return 0.0
        return numerator / denominator

    @staticmethod
    def _select_entry_signal(market_environment: str) -> str:
        return "breakout_confirmed" if market_environment == "trend" else "mean_reversion_signal"

    @staticmethod
    def _select_strategy_family(market_environment: str) -> str:
        return "breakout" if market_environment == "trend" else "mean_reversion"

    @staticmethod
    def _normalize_text(value: str, field_name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field_name} must not be blank")
        return normalized

    @staticmethod
    def _normalize_historical_rows(historical_rows: list[dict[str, object]]) -> list[dict[str, object]]:
        normalized_rows = [
            {
                "close": StrategyResearchEngine._extract_close(row),
                "timestamp": StrategyResearchEngine._normalize_timestamp(row.get("timestamp")),
            }
            for row in historical_rows
        ]
        return sorted(
            normalized_rows,
            key=lambda row: (
                row["timestamp"],
                row["close"],
            ),
        )

    @staticmethod
    def _canonicalize_historical_rows(historical_rows: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            {
                "close": row["close"],
                "timestamp": row["timestamp"].isoformat() if isinstance(row["timestamp"], datetime) else None,
            }
            for row in historical_rows
        ]

    @staticmethod
    def _normalize_timestamp(value: object) -> datetime:
        if value is None:
            raise ValueError("historical_rows timestamp values are required")
        if not isinstance(value, datetime):
            raise ValueError("historical_rows timestamp values must be datetimes")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("historical_rows timestamp values must be timezone-aware")
        return value.astimezone(UTC)

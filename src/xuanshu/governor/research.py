from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json

from xuanshu.contracts.research import ResearchTrigger, StrategyPackage
from xuanshu.governor.research_providers import ResearchProvider


@dataclass(slots=True)
class StrategyResearchEngine:
    provider: ResearchProvider | None = None

    def build_candidate_package(
        self,
        *,
        trigger: ResearchTrigger,
        symbol_scope: list[str],
        market_environment: str,
        historical_rows: list[dict[str, object]],
        research_reason: str,
    ) -> StrategyPackage:
        return self._build_candidate_package(
            trigger=trigger,
            symbol_scope=symbol_scope,
            market_environment=market_environment,
            historical_rows=historical_rows,
            research_reason=research_reason,
        )

    async def build_candidate_package_from_provider(
        self,
        *,
        trigger: ResearchTrigger,
        symbol_scope: list[str],
        market_environment: str,
        historical_rows: list[dict[str, object]],
        research_reason: str,
    ) -> StrategyPackage:
        if self.provider is None:
            raise RuntimeError("research provider is not configured")
        suggestion = await self.provider.generate_analysis(
            symbol_scope=symbol_scope,
            market_environment=market_environment,
            historical_rows=historical_rows,
            research_reason=research_reason,
        )
        provider_reason = f"{research_reason} | {self._normalize_text(suggestion.thesis, 'thesis')}"
        normalized_strategy_family = self._normalize_provider_strategy_family(
            suggestion.strategy_family,
            market_environment=market_environment,
        )
        normalized_entry_signal = self._normalize_provider_entry_signal(
            suggestion.entry_signal,
            strategy_family=normalized_strategy_family,
        )
        return self._build_candidate_package(
            trigger=trigger,
            symbol_scope=symbol_scope,
            market_environment=market_environment,
            historical_rows=historical_rows,
            research_reason=provider_reason,
            strategy_family=normalized_strategy_family,
            entry_signal=normalized_entry_signal,
            stop_loss_bps=suggestion.exit_stop_loss_bps,
            take_profit_bps=suggestion.exit_take_profit_bps,
            risk_fraction=suggestion.risk_fraction,
            max_hold_minutes=suggestion.max_hold_minutes,
            failure_modes=suggestion.failure_modes,
            invalidating_conditions=suggestion.invalidating_conditions,
        )

    def _build_candidate_package(
        self,
        *,
        trigger: ResearchTrigger,
        symbol_scope: list[str],
        market_environment: str,
        historical_rows: list[dict[str, object]],
        research_reason: str,
        strategy_family: str | None = None,
        entry_signal: str | None = None,
        stop_loss_bps: int = 50,
        take_profit_bps: int = 120,
        risk_fraction: float = 0.0025,
        max_hold_minutes: int = 60,
        failure_modes: list[str] | None = None,
        invalidating_conditions: list[str] | None = None,
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
            strategy_family=self._normalize_text(
                strategy_family or self._select_strategy_family(normalized_market_environment),
                "strategy_family",
            ),
            directionality="long_short",
            entry_rules={
                "signal": self._normalize_text(
                    entry_signal or self._select_entry_signal(normalized_market_environment),
                    "entry_signal",
                ),
            },
            exit_rules={
                "stop_loss_bps": stop_loss_bps,
                "take_profit_bps": take_profit_bps,
            },
            position_sizing_rules={
                "risk_fraction": risk_fraction,
            },
            risk_constraints={
                "max_hold_minutes": max_hold_minutes,
            },
            parameter_set=parameter_set,
            backtest_summary=backtest_summary,
            performance_summary=performance_summary,
            failure_modes=[self._normalize_text(item, "failure_modes") for item in (failure_modes or [])],
            invalidating_conditions=[
                self._normalize_text(item, "invalidating_conditions")
                for item in (invalidating_conditions or [])
            ],
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

    @classmethod
    def _normalize_provider_strategy_family(cls, value: str, *, market_environment: str) -> str:
        normalized = cls._normalize_free_text_label(value)
        if normalized == "breakout" or any(
            token in normalized for token in ("breakout", "trend", "momentum", "continuation", "follow")
        ):
            return "breakout"
        if normalized == "mean_reversion" or any(
            token in normalized for token in ("mean reversion", "mean_reversion", "reversion", "range")
        ):
            return "mean_reversion"
        return cls._select_strategy_family(market_environment)

    @classmethod
    def _normalize_provider_entry_signal(cls, value: str, *, strategy_family: str) -> str:
        normalized = cls._normalize_free_text_label(value)
        if strategy_family == "breakout":
            return "breakout_confirmed"
        if "range retest" in normalized or "range_retest" in normalized or "retest" in normalized:
            return "range_retest"
        return "mean_reversion_signal"

    @staticmethod
    def _normalize_free_text_label(value: str) -> str:
        return " ".join(value.strip().lower().replace("-", " ").replace("_", " ").split())

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

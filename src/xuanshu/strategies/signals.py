from __future__ import annotations

from collections.abc import Mapping, Sequence

from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.contracts.risk import CandidateSignal
from xuanshu.contracts.strategy_definition import StrategyDefinition
from xuanshu.core.enums import EntryType, MarketRegime, OrderSide, SignalUrgency, StrategyId
from xuanshu.strategies.dsl_execution import build_candidate_signal as build_dsl_candidate_signal


def build_candidate_signals(
    snapshot: MarketStateSnapshot,
    *,
    dsl_strategy_definitions: Sequence[StrategyDefinition] | None = None,
    historical_rows_by_strategy_def_id: Mapping[str, Sequence[dict[str, object]]] | None = None,
) -> list[CandidateSignal]:
    if dsl_strategy_definitions is None:
        dsl_strategy_definitions = getattr(snapshot, "dsl_strategy_definitions", None)
    if historical_rows_by_strategy_def_id is None:
        historical_rows_by_strategy_def_id = getattr(snapshot, "dsl_historical_rows_by_strategy_def_id", None)
    if dsl_strategy_definitions is not None:
        if historical_rows_by_strategy_def_id is None:
            raise ValueError("historical_rows_by_strategy_def_id is required for DSL strategy evaluation")
        return [
            signal
            for definition in dsl_strategy_definitions
            if (signal := build_dsl_candidate_signal(
                definition,
                historical_rows_by_strategy_def_id.get(definition.strategy_def_id, []),
            ))
            is not None
        ]

    if snapshot.regime == MarketRegime.TREND:
        return [
            CandidateSignal(
                symbol=snapshot.symbol,
                strategy_id=StrategyId.BREAKOUT,
                side=OrderSide.BUY,
                entry_type=EntryType.MARKET,
                urgency=SignalUrgency.HIGH,
                confidence=0.7,
                max_hold_ms=3000,
                cancel_after_ms=750,
                risk_tag="trend",
            )
        ]
    if snapshot.regime == MarketRegime.MEAN_REVERSION:
        return [
            CandidateSignal(
                symbol=snapshot.symbol,
                strategy_id=StrategyId.MEAN_REVERSION,
                side=OrderSide.SELL,
                entry_type=EntryType.MARKET,
                urgency=SignalUrgency.NORMAL,
                confidence=0.6,
                max_hold_ms=2000,
                cancel_after_ms=500,
                risk_tag="revert",
            )
        ]
    return [
        CandidateSignal(
            symbol=snapshot.symbol,
            strategy_id=StrategyId.RISK_PAUSE,
            side=OrderSide.FLAT,
            entry_type=EntryType.NONE,
            urgency=SignalUrgency.LOW,
            confidence=0.0,
            max_hold_ms=1,
            cancel_after_ms=1,
            risk_tag="pause",
        )
    ]

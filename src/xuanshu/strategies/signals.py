from __future__ import annotations

from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.contracts.risk import CandidateSignal
from xuanshu.core.enums import EntryType, MarketRegime, OrderSide, SignalUrgency, StrategyId


def build_candidate_signals(snapshot: MarketStateSnapshot) -> list[CandidateSignal]:
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

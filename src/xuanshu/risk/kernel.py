from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from xuanshu.contracts.risk import CandidateSignal, RiskDecision
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import RunMode, StrategyId


class RiskKernel:
    def __init__(self, nav: float) -> None:
        self.nav = nav

    def evaluate(self, signal: CandidateSignal, snapshot: StrategyConfigSnapshot) -> RiskDecision:
        allow_open = signal.strategy_id != StrategyId.RISK_PAUSE
        reason_codes: list[str] = []

        if snapshot.market_mode in {RunMode.REDUCE_ONLY, RunMode.HALTED}:
            allow_open = False
            reason_codes.append("mode_blocks_open")

        max_position = self.nav * snapshot.per_symbol_max_position * snapshot.risk_multiplier
        return RiskDecision(
            decision_id=str(uuid4()),
            generated_at=datetime.now(UTC),
            symbol=signal.symbol,
            allow_open=allow_open,
            allow_close=True,
            max_position=max_position,
            max_order_size=min(max_position, self.nav * 0.0035),
            risk_mode=snapshot.market_mode,
            reason_codes=reason_codes,
        )

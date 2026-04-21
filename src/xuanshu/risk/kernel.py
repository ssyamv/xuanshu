from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import uuid4

from xuanshu.contracts.risk import CandidateSignal, RiskDecision
from xuanshu.contracts.strategy import ApprovedStrategyBinding, StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, OrderSide, RunMode, StrategyId


class RiskKernel:
    def __init__(self, nav: float) -> None:
        self.nav = nav

    def evaluate(self, signal: CandidateSignal, snapshot: StrategyConfigSnapshot) -> RiskDecision:
        reference_time = datetime.now(UTC)
        allow_open = signal.strategy_id != StrategyId.RISK_PAUSE and signal.side != OrderSide.FLAT
        reason_codes: list[str] = []

        if snapshot.approval_state != ApprovalState.APPROVED:
            allow_open = False
            reason_codes.append("snapshot_not_approved")

        if not snapshot.is_effective(reference_time) or snapshot.is_expired(reference_time):
            allow_open = False
            reason_codes.append("snapshot_inactive")

        if not snapshot.allows_symbol(signal.symbol):
            allow_open = False
            reason_codes.append("symbol_not_whitelisted")

        if signal.strategy_id != StrategyId.RISK_PAUSE and not snapshot.is_strategy_enabled(signal.strategy_id):
            allow_open = False
            reason_codes.append("strategy_disabled")

        if snapshot.market_mode in {RunMode.REDUCE_ONLY, RunMode.HALTED}:
            allow_open = False
            reason_codes.append("mode_blocks_open")

        if signal.side == OrderSide.FLAT:
            reason_codes.append("pause_signal")

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


def is_stronger_strategy_replacement(
    current: ApprovedStrategyBinding | None,
    candidate: ApprovedStrategyBinding,
) -> bool:
    if current is None:
        return True

    current_score = _coerce_strategy_score(current.score)
    candidate_score = _coerce_strategy_score(candidate.score)
    if current_score is None or candidate_score is None:
        return False

    current_basis = _normalize_score_basis(current.score_basis)
    candidate_basis = _normalize_score_basis(candidate.score_basis)
    if current_basis is None or candidate_basis is None:
        return False
    if current_basis != candidate_basis:
        return False

    return candidate_score + 1e-12 >= current_score * 1.10


def _normalize_score_basis(score_basis: object) -> str | None:
    if not isinstance(score_basis, str):
        return None
    normalized = score_basis.strip().lower()
    return normalized or None


def _coerce_strategy_score(score: object) -> float | None:
    if isinstance(score, bool):
        return None
    if not isinstance(score, (int, float)):
        return None
    numeric_score = float(score)
    if not math.isfinite(numeric_score):
        return None
    return numeric_score

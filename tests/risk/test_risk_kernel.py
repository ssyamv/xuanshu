from datetime import UTC, datetime, timedelta

import pytest

from xuanshu.contracts.risk import CandidateSignal
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, EntryType, OrderSide, RunMode, SignalUrgency, StrategyId
from xuanshu.risk.kernel import RiskKernel


def _build_signal(
    *,
    symbol: str = "BTC-USDT-SWAP",
    strategy_id: StrategyId = StrategyId.VOL_BREAKOUT,
    side: OrderSide = OrderSide.BUY,
) -> CandidateSignal:
    return CandidateSignal(
        symbol=symbol,
        strategy_id=strategy_id,
        side=side,
        entry_type=EntryType.MARKET,
        urgency=SignalUrgency.HIGH,
        confidence=0.7,
        max_hold_ms=3_000,
        cancel_after_ms=750,
        risk_tag="vol_breakout",
    )


def _build_snapshot(
    *,
    effective_from: datetime | None = None,
    expires_at: datetime | None = None,
    symbol_whitelist: list[str] | None = None,
    strategy_enable_flags: dict[str, bool] | None = None,
    approval_state: ApprovalState = ApprovalState.APPROVED,
    market_mode: RunMode = RunMode.NORMAL,
) -> StrategyConfigSnapshot:
    now = datetime.now(UTC)
    return StrategyConfigSnapshot(
        version_id="snap-001",
        generated_at=now,
        effective_from=effective_from or now - timedelta(minutes=1),
        expires_at=expires_at or now + timedelta(minutes=5),
        symbol_whitelist=symbol_whitelist or ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        strategy_enable_flags=strategy_enable_flags
        or {
            StrategyId.VOL_BREAKOUT.value: True,
            StrategyId.SHORT_MOMENTUM.value: True,
            StrategyId.RISK_PAUSE.value: True,
        },
        risk_multiplier=0.8,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=market_mode,
        approval_state=approval_state,
        source_reason="committee result",
        ttl_sec=300,
    )


@pytest.mark.parametrize(
    ("signal", "snapshot_factory", "expected_reason"),
    [
        (
            _build_signal(symbol="SOL-USDT-SWAP"),
            lambda: _build_snapshot(),
            "symbol_not_whitelisted",
        ),
        (
            _build_signal(strategy_id=StrategyId.VOL_BREAKOUT),
            lambda: _build_snapshot(strategy_enable_flags={StrategyId.VOL_BREAKOUT.value: False}),
            "strategy_disabled",
        ),
        (
            _build_signal(),
            lambda: _build_snapshot(approval_state=ApprovalState.REJECTED),
            "snapshot_not_approved",
        ),
        (
            _build_signal(),
            lambda: _build_snapshot(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
            "snapshot_inactive",
        ),
        (
            _build_signal(),
            lambda: _build_snapshot(effective_from=datetime.now(UTC) + timedelta(minutes=1)),
            "snapshot_inactive",
        ),
    ],
)
def test_risk_kernel_blocks_open_when_governance_snapshot_disallows_it(
    signal: CandidateSignal,
    snapshot_factory,
    expected_reason: str,
) -> None:
    snapshot = snapshot_factory()
    decision = RiskKernel(nav=100_000.0).evaluate(signal, snapshot)

    assert decision.allow_open is False
    assert expected_reason in decision.reason_codes
    assert decision.allow_close is True


def test_risk_kernel_allows_enabled_short_momentum_sell_signal() -> None:
    decision = RiskKernel(nav=100_000.0).evaluate(
        _build_signal(strategy_id=StrategyId.SHORT_MOMENTUM, side=OrderSide.SELL),
        _build_snapshot(),
    )

    assert decision.allow_open is True
    assert decision.reason_codes == []

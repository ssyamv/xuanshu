from datetime import UTC, datetime, timedelta

from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.governor.service import GovernorService


def test_governor_keeps_last_valid_snapshot_when_ai_fails() -> None:
    snapshot = StrategyConfigSnapshot(
        version_id="snap-last",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.7,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="cached",
        ttl_sec=300,
    )

    service = GovernorService()

    assert service.freeze_on_failure(snapshot).version_id == "snap-last"

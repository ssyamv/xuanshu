from datetime import UTC, datetime, timedelta

from xuanshu.contracts.governance import ExpertOpinion
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import RunMode


def test_strategy_snapshot_and_expert_opinion_are_stable_contracts() -> None:
    snapshot = StrategyConfigSnapshot(
        version_id="snap-001",
        generated_at=datetime.now(UTC),
        effective_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": True, "risk_pause": True},
        risk_multiplier=0.8,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state="approved",
        source_reason="committee result",
        ttl_sec=300,
    )
    opinion = ExpertOpinion(
        opinion_id="op-001",
        expert_type="risk",
        generated_at=datetime.now(UTC),
        symbol_scope=["BTC-USDT-SWAP"],
        decision="tighten_risk",
        confidence=0.8,
        supporting_facts=["recent risk events rising"],
        risk_flags=["drawdown_watch"],
        ttl_sec=300,
    )

    assert snapshot.is_expired(datetime.now(UTC)) is False
    assert opinion.expert_type == "risk"

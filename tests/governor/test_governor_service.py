from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.infra.ai.governor_client import GovernorClient
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

    frozen_snapshot = service.freeze_on_failure(snapshot)

    assert frozen_snapshot.version_id == "snap-last"
    assert frozen_snapshot is not snapshot

    frozen_snapshot.symbol_whitelist.append("ETH-USDT-SWAP")

    assert snapshot.symbol_whitelist == ["BTC-USDT-SWAP"]


class _BrokenGovernorRunner:
    async def run(self, state_summary: dict[str, object]) -> dict[str, object]:
        return {"version_id": state_summary["version_id"]}


@pytest.mark.asyncio
async def test_governor_client_validates_agent_output() -> None:
    client = GovernorClient(agent_runner=_BrokenGovernorRunner())

    with pytest.raises(ValidationError):
        await client.generate_snapshot({"version_id": "snap-invalid"})

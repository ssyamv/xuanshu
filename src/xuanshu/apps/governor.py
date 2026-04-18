from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from xuanshu.config.settings import GovernorRuntimeSettings
from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.governor.service import GovernorService
from xuanshu.infra.ai.governor_client import ConfiguredGovernorAgentRunner, GovernorClient


@dataclass(slots=True)
class GovernorRuntime:
    settings: GovernorRuntimeSettings
    service: GovernorService
    governor_client: GovernorClient
    last_snapshot: StrategyConfigSnapshot
    published_snapshots: list[StrategyConfigSnapshot] = field(default_factory=list)


def build_governor_service() -> GovernorService:
    return GovernorService()


def build_governor_client(settings: GovernorRuntimeSettings) -> GovernorClient:
    return GovernorClient(
        agent_runner=ConfiguredGovernorAgentRunner(
            api_key=settings.openai_api_key,
            timeout_sec=settings.ai_timeout_sec,
        )
    )


def _build_bootstrap_snapshot() -> StrategyConfigSnapshot:
    generated_at = datetime.now(UTC)
    return StrategyConfigSnapshot(
        version_id="bootstrap",
        generated_at=generated_at,
        effective_from=generated_at,
        expires_at=generated_at + timedelta(minutes=5),
        symbol_whitelist=["BTC-USDT-SWAP"],
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.5,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=RunMode.NORMAL,
        approval_state=ApprovalState.APPROVED,
        source_reason="bootstrap",
        ttl_sec=300,
    )


def build_governor_runtime() -> GovernorRuntime:
    settings = GovernorRuntimeSettings()
    return GovernorRuntime(
        settings=settings,
        service=build_governor_service(),
        governor_client=build_governor_client(settings),
        last_snapshot=_build_bootstrap_snapshot(),
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _run_governor(runtime: GovernorRuntime) -> None:
    runtime.last_snapshot = await runtime.service.run_cycle(
        state_summary={"scope": "governor"},
        last_snapshot=runtime.last_snapshot,
        governor_client=runtime.governor_client,
        publish_snapshot=runtime.published_snapshots.append,
    )
    await _wait_forever()


def main() -> int:
    asyncio.run(_run_governor(build_governor_runtime()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

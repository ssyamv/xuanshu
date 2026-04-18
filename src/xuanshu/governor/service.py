from __future__ import annotations

from collections.abc import Callable, Mapping

from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.infra.ai.governor_client import GovernorClient


class GovernorService:
    def freeze_on_failure(self, last_snapshot: StrategyConfigSnapshot) -> StrategyConfigSnapshot:
        return last_snapshot.model_copy(deep=True)

    async def run_cycle(
        self,
        state_summary: Mapping[str, object],
        last_snapshot: StrategyConfigSnapshot,
        governor_client: GovernorClient,
        publish_snapshot: Callable[[StrategyConfigSnapshot], None],
    ) -> StrategyConfigSnapshot:
        try:
            snapshot = await governor_client.generate_snapshot(state_summary)
        except Exception:
            snapshot = self.freeze_on_failure(last_snapshot)

        publish_snapshot(snapshot)
        return snapshot

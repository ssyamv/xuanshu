from __future__ import annotations

from dataclasses import dataclass

from xuanshu.contracts.strategy import StrategyConfigSnapshot
from xuanshu.infra.ai.governor_client import GovernorClient


@dataclass(frozen=True, slots=True)
class GovernorService:
    client: GovernorClient | None = None

    def freeze_on_failure(self, last_snapshot: StrategyConfigSnapshot) -> StrategyConfigSnapshot:
        return last_snapshot.model_copy(deep=True)

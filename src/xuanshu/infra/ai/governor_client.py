from collections.abc import Mapping
from typing import Protocol

from xuanshu.contracts.strategy import StrategyConfigSnapshot


class GovernorAgentRunner(Protocol):
    async def run(self, state_summary: Mapping[str, object]) -> object:
        ...


class GovernorClient:
    def __init__(self, agent_runner: GovernorAgentRunner) -> None:
        self.agent_runner = agent_runner

    async def generate_snapshot(self, state_summary: Mapping[str, object]) -> StrategyConfigSnapshot:
        result = await self.agent_runner.run(state_summary)
        return StrategyConfigSnapshot.model_validate(result)

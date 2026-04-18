from dataclasses import dataclass
from collections.abc import Mapping
from typing import Protocol
from pydantic import SecretStr

from xuanshu.contracts.strategy import StrategyConfigSnapshot


class GovernorAgentRunner(Protocol):
    async def run(self, state_summary: Mapping[str, object]) -> object:
        ...


@dataclass(frozen=True, slots=True)
class ConfiguredGovernorAgentRunner:
    api_key: SecretStr
    timeout_sec: int

    async def run(self, state_summary: Mapping[str, object]) -> object:
        raise NotImplementedError("configured governor agent runner is not implemented in the skeleton")


class GovernorClient:
    def __init__(self, agent_runner: GovernorAgentRunner) -> None:
        self.agent_runner = agent_runner

    async def generate_snapshot(self, state_summary: Mapping[str, object]) -> StrategyConfigSnapshot:
        result = await self.agent_runner.run(state_summary)
        return StrategyConfigSnapshot.model_validate(result)

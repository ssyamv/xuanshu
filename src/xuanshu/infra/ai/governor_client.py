from xuanshu.contracts.strategy import StrategyConfigSnapshot


class GovernorClient:
    def __init__(self, agent_runner) -> None:
        self.agent_runner = agent_runner

    async def generate_snapshot(self, state_summary: dict[str, object]) -> StrategyConfigSnapshot:
        result = await self.agent_runner.run(state_summary)
        return StrategyConfigSnapshot.model_validate(result)

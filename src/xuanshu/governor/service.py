from xuanshu.contracts.strategy import StrategyConfigSnapshot


class GovernorService:
    def freeze_on_failure(self, last_snapshot: StrategyConfigSnapshot) -> StrategyConfigSnapshot:
        return last_snapshot.model_copy(deep=True)

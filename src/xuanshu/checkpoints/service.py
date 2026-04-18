from __future__ import annotations

from xuanshu.contracts.checkpoint import ExecutionCheckpoint


class CheckpointService:
    def can_open_new_risk(self, checkpoint: ExecutionCheckpoint) -> bool:
        if checkpoint.needs_reconcile:
            return False

        budget = checkpoint.budget_state
        if budget.remaining_daily_loss <= 0:
            return False
        if budget.remaining_notional <= 0:
            return False
        if budget.remaining_order_count <= 0:
            return False

        return True

from __future__ import annotations

from xuanshu.contracts.checkpoint import ExecutionCheckpoint


class CheckpointService:
    def can_open_new_risk(self, checkpoint: ExecutionCheckpoint) -> bool:
        return not checkpoint.needs_reconcile

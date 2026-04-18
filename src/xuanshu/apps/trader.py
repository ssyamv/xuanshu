from xuanshu.checkpoints.service import CheckpointService
from xuanshu.execution.engine import build_client_order_id
from xuanshu.risk.kernel import RiskKernel
from xuanshu.state.engine import StateEngine


def build_trader_components() -> dict[str, object]:
    return {
        "state_engine": StateEngine(),
        "risk_kernel": RiskKernel(nav=100_000.0),
        "checkpoint_service": CheckpointService(),
        "client_order_id_builder": build_client_order_id,
    }

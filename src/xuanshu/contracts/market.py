from datetime import datetime

from pydantic import BaseModel

from xuanshu.core.enums import RunMode


class MarketStateSnapshot(BaseModel):
    snapshot_id: str
    generated_at: datetime
    symbol: str
    mid_price: float
    spread: float
    imbalance: float
    recent_trade_bias: float
    volatility_state: str
    regime: str
    current_position: float
    current_mode: RunMode
    risk_budget_remaining: float

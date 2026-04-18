from datetime import datetime

from pydantic import BaseModel, Field

from xuanshu.core.enums import MarketRegime, RunMode, VolatilityState


class MarketStateSnapshot(BaseModel):
    snapshot_id: str = Field(min_length=1)
    generated_at: datetime
    symbol: str = Field(min_length=1)
    mid_price: float = Field(ge=0.0)
    spread: float = Field(ge=0.0)
    imbalance: float = Field(ge=-1.0, le=1.0)
    recent_trade_bias: float = Field(ge=-1.0, le=1.0)
    volatility_state: VolatilityState
    regime: MarketRegime
    current_position: float
    current_mode: RunMode
    risk_budget_remaining: float = Field(ge=0.0)

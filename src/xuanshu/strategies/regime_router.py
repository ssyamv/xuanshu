from __future__ import annotations

from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.core.enums import MarketRegime, VolatilityState


def classify_regime(snapshot: MarketStateSnapshot) -> MarketRegime:
    if abs(snapshot.recent_trade_bias) > 0.6 and snapshot.volatility_state == VolatilityState.HOT:
        return MarketRegime.TREND
    if abs(snapshot.recent_trade_bias) < 0.2 and snapshot.volatility_state == VolatilityState.NORMAL:
        return MarketRegime.MEAN_REVERSION
    if snapshot.spread > 0.5 or abs(snapshot.imbalance) > 0.9:
        return MarketRegime.UNKNOWN
    return MarketRegime.RANGE

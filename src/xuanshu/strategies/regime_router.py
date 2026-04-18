from __future__ import annotations

from xuanshu.contracts.market import MarketStateSnapshot


def classify_regime(snapshot: MarketStateSnapshot) -> str:
    if snapshot.recent_trade_bias > 0.6 and snapshot.volatility_state == "expanding":
        return "trend_expansion"
    if abs(snapshot.recent_trade_bias) < 0.2 and snapshot.volatility_state == "contained":
        return "mean_reversion"
    if snapshot.spread > 0.5 or abs(snapshot.imbalance) > 0.9:
        return "abnormal"
    return "neutral"

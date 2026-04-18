from enum import StrEnum


class RunMode(StrEnum):
    NORMAL = "normal"
    DEGRADED = "degraded"
    REDUCE_ONLY = "reduce_only"
    HALTED = "halted"


class StrategyId(StrEnum):
    BREAKOUT = "breakout"
    MEAN_REVERSION = "mean_reversion"
    RISK_PAUSE = "risk_pause"


class EventType(StrEnum):
    MARKET = "market"
    ORDER = "order"
    POSITION = "position"

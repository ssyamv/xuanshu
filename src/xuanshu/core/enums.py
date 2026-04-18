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


class ApprovalState(StrEnum):
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"
    EXPIRED = "expired"


class MarketRegime(StrEnum):
    BREAKOUT = "breakout"
    MEAN_REVERSION = "mean_reversion"
    RANGE = "range"
    TREND = "trend"
    UNKNOWN = "unknown"


class VolatilityState(StrEnum):
    QUIET = "quiet"
    NORMAL = "normal"
    HOT = "hot"
    STRESSED = "stressed"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class EntryType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class SignalUrgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    IMMEDIATE = "immediate"

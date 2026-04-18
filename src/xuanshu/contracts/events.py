from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from xuanshu.core.enums import TraderEventType

NormalizedStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SequenceId = NormalizedStr


class _TraderEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: TraderEventType
    exchange: NormalizedStr
    generated_at: datetime

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(UTC)


class OrderbookTopEvent(_TraderEvent):
    event_type: Literal[TraderEventType.ORDERBOOK_TOP]
    symbol: NormalizedStr
    public_sequence: SequenceId
    bid_price: float = Field(ge=0.0)
    ask_price: float = Field(ge=0.0)
    bid_size: float = Field(ge=0.0)
    ask_size: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_prices(self) -> "OrderbookTopEvent":
        if self.ask_price < self.bid_price:
            raise ValueError("ask_price must be >= bid_price")
        return self


class MarketTradeEvent(_TraderEvent):
    event_type: Literal[TraderEventType.MARKET_TRADE]
    symbol: NormalizedStr
    public_sequence: SequenceId
    price: float = Field(ge=0.0)
    size: float = Field(gt=0.0)
    side: Literal["buy", "sell"]


class OrderUpdateEvent(_TraderEvent):
    event_type: Literal[TraderEventType.ORDER_UPDATE]
    symbol: NormalizedStr
    private_sequence: SequenceId
    order_id: NormalizedStr
    client_order_id: NormalizedStr
    side: Literal["buy", "sell"]
    price: float = Field(ge=0.0)
    size: float = Field(gt=0.0)
    filled_size: float = Field(ge=0.0)
    status: NormalizedStr

    @model_validator(mode="after")
    def validate_filled_size(self) -> "OrderUpdateEvent":
        if self.filled_size > self.size:
            raise ValueError("filled_size must be <= size")
        return self


class PositionUpdateEvent(_TraderEvent):
    event_type: Literal[TraderEventType.POSITION_UPDATE]
    symbol: NormalizedStr
    private_sequence: SequenceId
    net_quantity: float
    average_price: float = Field(ge=0.0)
    mark_price: float = Field(ge=0.0)
    unrealized_pnl: float


class AccountSnapshotEvent(_TraderEvent):
    event_type: Literal[TraderEventType.ACCOUNT_SNAPSHOT]
    private_sequence: SequenceId
    equity: float = Field(ge=0.0)
    available_balance: float = Field(ge=0.0)
    margin_ratio: float = Field(ge=0.0)


class FaultEvent(_TraderEvent):
    event_type: Literal[TraderEventType.RUNTIME_FAULT]
    severity: Literal["info", "warn", "critical"]
    code: NormalizedStr
    detail: NormalizedStr

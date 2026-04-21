from __future__ import annotations

from xuanshu.contracts.events import (
    AccountSnapshotEvent,
    FaultEvent,
    MarketTradeEvent,
    OrderUpdateEvent,
    OrderbookTopEvent,
    PositionUpdateEvent,
)
from xuanshu.state.engine import StateEngine

_STRATEGY_HANDOVER_EVENT_ORDER = (
    "cancel_open_orders",
    "flatten_position",
    "mark_replaced_by_stronger_strategy",
    "activate_new_strategy",
)


def dispatch_event(engine: StateEngine, event: object) -> None:
    if isinstance(event, OrderbookTopEvent):
        engine.on_orderbook_top(event)
        return
    if isinstance(event, MarketTradeEvent):
        engine.on_market_trade(event)
        return
    if isinstance(event, OrderUpdateEvent):
        engine.on_order_update(event)
        return
    if isinstance(event, PositionUpdateEvent):
        engine.on_position_update(event)
        return
    if isinstance(event, AccountSnapshotEvent):
        engine.on_account_snapshot(event)
        return
    if isinstance(event, FaultEvent):
        engine.on_fault(event)
        return
    raise ValueError(f"unsupported event type: {type(event).__name__}")


def build_strategy_handover_event_order() -> tuple[str, ...]:
    return _STRATEGY_HANDOVER_EVENT_ORDER

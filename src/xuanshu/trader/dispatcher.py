from __future__ import annotations

from xuanshu.contracts.events import FaultEvent, OrderUpdateEvent, OrderbookTopEvent, PositionUpdateEvent
from xuanshu.state.engine import StateEngine


def dispatch_event(engine: StateEngine, event: object) -> None:
    if isinstance(event, OrderbookTopEvent):
        engine.on_orderbook_top(event)
        return
    if isinstance(event, OrderUpdateEvent):
        engine.on_order_update(event)
        return
    if isinstance(event, PositionUpdateEvent):
        engine.on_position_update(event)
        return
    if isinstance(event, FaultEvent):
        engine.on_fault(event)
        return

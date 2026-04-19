from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from xuanshu.contracts.events import FaultEvent, OrderUpdateEvent, OrderbookTopEvent, PositionUpdateEvent
from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.core.enums import MarketRegime, RunMode, VolatilityState
from xuanshu.strategies.regime_router import classify_regime

_TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "cancelled", "rejected"})


@dataclass
class SymbolState:
    bid: float | None = None
    ask: float | None = None
    recent_trade_sides: deque[str] = field(default_factory=deque)


@dataclass
class OrderState:
    order_id: str
    client_order_id: str
    side: str
    price: float
    size: float
    filled_size: float
    status: str


@dataclass
class PositionState:
    net_quantity: float = 0.0
    average_price: float = 0.0
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class StateEngine:
    symbols: dict[str, SymbolState] = field(default_factory=dict)
    open_orders_by_symbol: dict[str, dict[str, OrderState]] = field(default_factory=dict)
    positions_by_symbol: dict[str, PositionState] = field(default_factory=dict)
    fault_flags: dict[str, dict[str, str]] = field(default_factory=dict)
    current_run_mode: RunMode = RunMode.NORMAL
    last_public_stream_marker: str | None = None
    last_private_stream_marker: str | None = None
    recent_trade_window: int = 20

    def set_run_mode(self, mode: RunMode) -> None:
        self.current_run_mode = mode

    def on_bbo(self, symbol: str, bid: float, ask: float) -> None:
        state = self.symbols.setdefault(symbol, SymbolState())
        state.bid = bid
        state.ask = ask

    def on_orderbook_top(self, event: OrderbookTopEvent) -> None:
        self.last_public_stream_marker = event.public_sequence
        self.on_bbo(event.symbol, bid=event.bid_price, ask=event.ask_price)

    def on_trade(self, symbol: str, price: float, size: float, side: str) -> None:
        state = self.symbols.setdefault(symbol, SymbolState())
        normalized_side = side.lower()
        if normalized_side not in {"buy", "sell"}:
            raise ValueError(f"unsupported trade side: {side}")
        state.recent_trade_sides.append(normalized_side)
        while len(state.recent_trade_sides) > self.recent_trade_window:
            state.recent_trade_sides.popleft()

    def on_order_update(self, event: OrderUpdateEvent) -> None:
        self.last_private_stream_marker = event.private_sequence
        symbol_orders = self.open_orders_by_symbol.setdefault(event.symbol, {})
        normalized_status = event.status.strip().lower()
        symbol_orders[event.order_id] = OrderState(
            order_id=event.order_id,
            client_order_id=event.client_order_id,
            side=event.side,
            price=event.price,
            size=event.size,
            filled_size=event.filled_size,
            status=normalized_status,
        )
        if normalized_status in _TERMINAL_ORDER_STATUSES:
            symbol_orders.pop(event.order_id, None)

    def on_position_update(self, event: PositionUpdateEvent) -> None:
        self.last_private_stream_marker = event.private_sequence
        self.positions_by_symbol[event.symbol] = PositionState(
            net_quantity=event.net_quantity,
            average_price=event.average_price,
            mark_price=event.mark_price,
            unrealized_pnl=event.unrealized_pnl,
        )

    def on_fault(self, event: FaultEvent) -> None:
        self.fault_flags[event.code] = {
            "severity": event.severity,
            "detail": event.detail,
        }

    def build_symbol_runtime_summary(self, symbol: str) -> dict[str, object]:
        position = self.positions_by_symbol.get(symbol, PositionState())
        open_orders = self.open_orders_by_symbol.get(symbol, {})
        snapshot = self.snapshot(symbol)
        return {
            "symbol": symbol,
            "run_mode": self.current_run_mode.value,
            "mid_price": snapshot.mid_price,
            "spread": snapshot.spread,
            "regime": snapshot.regime.value,
            "net_quantity": position.net_quantity,
            "open_order_count": len(open_orders),
            "fault_count": len(self.fault_flags),
        }

    def snapshot(self, symbol: str) -> MarketStateSnapshot:
        state = self.symbols.setdefault(symbol, SymbolState())
        has_quotes = state.bid is not None and state.ask is not None and state.bid > 0.0 and state.ask >= state.bid
        if not has_quotes:
            return MarketStateSnapshot(
                snapshot_id=str(uuid4()),
                generated_at=datetime.now(UTC),
                symbol=symbol,
                mid_price=0.0,
                spread=0.0,
                imbalance=0.0,
                recent_trade_bias=0.0,
                volatility_state=VolatilityState.NORMAL,
                regime=MarketRegime.UNKNOWN,
                current_position=self.positions_by_symbol.get(symbol, PositionState()).net_quantity,
                current_mode=self.current_run_mode,
                risk_budget_remaining=1.0,
            )

        mid_price = (state.bid + state.ask) / 2
        spread = max(state.ask - state.bid, 0.0)
        buys = sum(1 for item in state.recent_trade_sides if item == "buy")
        sells = sum(1 for item in state.recent_trade_sides if item == "sell")
        total_trades = buys + sells
        if total_trades == 0:
            recent_trade_bias = 0.0
            regime = MarketRegime.UNKNOWN
        else:
            recent_trade_bias = (buys - sells) / total_trades
            snapshot = MarketStateSnapshot(
                snapshot_id=str(uuid4()),
                generated_at=datetime.now(UTC),
                symbol=symbol,
                mid_price=mid_price,
                spread=spread,
                imbalance=recent_trade_bias,
                recent_trade_bias=recent_trade_bias,
                volatility_state=VolatilityState.HOT if spread >= 0.2 else VolatilityState.NORMAL,
                regime=MarketRegime.UNKNOWN,
                current_position=self.positions_by_symbol.get(symbol, PositionState()).net_quantity,
                current_mode=self.current_run_mode,
                risk_budget_remaining=1.0,
            )
            snapshot.regime = classify_regime(snapshot)
            return snapshot

        return MarketStateSnapshot(
            snapshot_id=str(uuid4()),
            generated_at=datetime.now(UTC),
            symbol=symbol,
            mid_price=mid_price,
            spread=spread,
            imbalance=recent_trade_bias,
            recent_trade_bias=recent_trade_bias,
            volatility_state=VolatilityState.HOT if spread >= 0.2 else VolatilityState.NORMAL,
            regime=regime,
            current_position=self.positions_by_symbol.get(symbol, PositionState()).net_quantity,
            current_mode=self.current_run_mode,
            risk_budget_remaining=1.0,
        )

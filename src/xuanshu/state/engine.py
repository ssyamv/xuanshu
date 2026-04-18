from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.core.enums import MarketRegime, RunMode, VolatilityState
from xuanshu.strategies.regime_router import classify_regime


@dataclass
class SymbolState:
    bid: float | None = None
    ask: float | None = None
    recent_trade_sides: deque[str] = field(default_factory=deque)


@dataclass
class StateEngine:
    symbols: dict[str, SymbolState] = field(default_factory=dict)
    recent_trade_window: int = 20

    def on_bbo(self, symbol: str, bid: float, ask: float) -> None:
        state = self.symbols.setdefault(symbol, SymbolState())
        state.bid = bid
        state.ask = ask

    def on_trade(self, symbol: str, price: float, size: float, side: str) -> None:
        state = self.symbols.setdefault(symbol, SymbolState())
        normalized_side = side.lower()
        if normalized_side not in {"buy", "sell"}:
            raise ValueError(f"unsupported trade side: {side}")
        state.recent_trade_sides.append(normalized_side)
        while len(state.recent_trade_sides) > self.recent_trade_window:
            state.recent_trade_sides.popleft()

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
                current_position=0.0,
                current_mode=RunMode.NORMAL,
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
                current_position=0.0,
                current_mode=RunMode.NORMAL,
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
            current_position=0.0,
            current_mode=RunMode.NORMAL,
            risk_budget_remaining=1.0,
        )

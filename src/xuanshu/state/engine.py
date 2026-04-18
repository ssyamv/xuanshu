from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from xuanshu.contracts.market import MarketStateSnapshot
from xuanshu.core.enums import MarketRegime, RunMode, VolatilityState
from xuanshu.strategies.regime_router import classify_regime


@dataclass
class SymbolState:
    bid: float = 0.0
    ask: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0


@dataclass
class StateEngine:
    symbols: dict[str, SymbolState] = field(default_factory=dict)

    def on_bbo(self, symbol: str, bid: float, ask: float) -> None:
        state = self.symbols.setdefault(symbol, SymbolState())
        state.bid = bid
        state.ask = ask

    def on_trade(self, symbol: str, price: float, size: float, side: str) -> None:
        state = self.symbols.setdefault(symbol, SymbolState())
        normalized_side = side.lower()
        if normalized_side == "buy":
            state.buy_volume += size
        elif normalized_side == "sell":
            state.sell_volume += size
        else:
            raise ValueError(f"unsupported trade side: {side}")

    def snapshot(self, symbol: str) -> MarketStateSnapshot:
        state = self.symbols[symbol]
        mid_price = (state.bid + state.ask) / 2
        total_volume = max(state.buy_volume + state.sell_volume, 1.0)
        recent_trade_bias = (state.buy_volume - state.sell_volume) / total_volume
        spread = max(state.ask - state.bid, 0.0)

        snapshot = MarketStateSnapshot(
            snapshot_id=str(uuid4()),
            generated_at=datetime.now(UTC),
            symbol=symbol,
            mid_price=mid_price,
            spread=spread,
            imbalance=recent_trade_bias,
            recent_trade_bias=recent_trade_bias,
            volatility_state=VolatilityState.NORMAL,
            regime=MarketRegime.UNKNOWN,
            current_position=0.0,
            current_mode=RunMode.NORMAL,
            risk_budget_remaining=1.0,
        )

        snapshot.volatility_state = VolatilityState.HOT if spread >= 0.2 else VolatilityState.NORMAL
        snapshot.regime = classify_regime(snapshot)
        return snapshot

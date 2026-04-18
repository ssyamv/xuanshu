from __future__ import annotations

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
    recent_trades: list[float] = field(default_factory=list)


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
        if normalized_side == "buy":
            signed_size = size
        elif normalized_side == "sell":
            signed_size = -size
        else:
            raise ValueError(f"unsupported trade side: {side}")
        state.recent_trades.append(signed_size)
        if len(state.recent_trades) > self.recent_trade_window:
            del state.recent_trades[0 : len(state.recent_trades) - self.recent_trade_window]

    def snapshot(self, symbol: str) -> MarketStateSnapshot:
        state = self.symbols.setdefault(symbol, SymbolState())
        has_quotes = state.bid is not None and state.ask is not None and state.bid > 0.0 and state.ask >= state.bid
        mid_price = ((state.bid + state.ask) / 2) if has_quotes else 0.0
        observed_volume = sum(abs(size) for size in state.recent_trades)
        directional_volume = sum(state.recent_trades)
        total_volume = max(observed_volume, 1.0)
        recent_trade_bias = directional_volume / total_volume
        spread = max(state.ask - state.bid, 0.0) if has_quotes else 0.0

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
        if not has_quotes or observed_volume <= 0.0:
            snapshot.regime = MarketRegime.UNKNOWN
            return snapshot
        snapshot.regime = classify_regime(snapshot)
        return snapshot

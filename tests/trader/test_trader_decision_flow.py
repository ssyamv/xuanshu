import pytest

from xuanshu.core.enums import EntryType, MarketRegime, OrderSide, SignalUrgency, StrategyId, VolatilityState
from xuanshu.state.engine import StateEngine
from xuanshu.strategies.signals import build_candidate_signals


def test_trader_generates_breakout_signal_for_trend_expansion() -> None:
    engine = StateEngine()
    engine.on_bbo("BTC-USDT-SWAP", bid=100.0, ask=100.2)
    engine.on_trade("BTC-USDT-SWAP", price=100.3, size=5.0, side="buy")
    engine.on_trade("BTC-USDT-SWAP", price=100.4, size=4.0, side="buy")

    snapshot = engine.snapshot("BTC-USDT-SWAP")
    signals = build_candidate_signals(snapshot)

    assert snapshot.volatility_state == VolatilityState.HOT
    assert snapshot.regime == MarketRegime.TREND
    assert signals[0].strategy_id == StrategyId.BREAKOUT
    assert signals[0].side == OrderSide.BUY
    assert signals[0].entry_type == EntryType.MARKET


def test_trader_pause_signal_is_explicitly_non_executable() -> None:
    engine = StateEngine()
    engine.on_bbo("BTC-USDT-SWAP", bid=100.0, ask=100.9)
    snapshot = engine.snapshot("BTC-USDT-SWAP")

    signals = build_candidate_signals(snapshot)

    assert signals[0].strategy_id == StrategyId.RISK_PAUSE
    assert signals[0].side == OrderSide.FLAT
    assert signals[0].entry_type == EntryType.NONE
    assert signals[0].urgency == SignalUrgency.LOW
    assert signals[0].confidence == 0.0


def test_trader_quote_only_cold_start_does_not_generate_a_live_entry_signal() -> None:
    engine = StateEngine()
    engine.on_bbo("BTC-USDT-SWAP", bid=100.0, ask=100.1)

    snapshot = engine.snapshot("BTC-USDT-SWAP")
    signals = build_candidate_signals(snapshot)

    assert snapshot.regime == MarketRegime.UNKNOWN
    assert signals[0].strategy_id == StrategyId.RISK_PAUSE
    assert signals[0].side == OrderSide.FLAT


def test_trader_trade_only_cold_start_does_not_generate_a_live_entry_signal() -> None:
    engine = StateEngine()
    engine.on_trade("BTC-USDT-SWAP", price=100.3, size=5.0, side="buy")
    engine.on_trade("BTC-USDT-SWAP", price=100.4, size=5.0, side="sell")

    snapshot = engine.snapshot("BTC-USDT-SWAP")
    signals = build_candidate_signals(snapshot)

    assert snapshot.regime == MarketRegime.UNKNOWN
    assert signals[0].strategy_id == StrategyId.RISK_PAUSE
    assert signals[0].side == OrderSide.FLAT


def test_trader_rejects_unsupported_trade_side() -> None:
    engine = StateEngine()

    with pytest.raises(ValueError, match="unsupported trade side"):
        engine.on_trade("BTC-USDT-SWAP", price=100.0, size=1.0, side="hold")

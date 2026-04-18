from xuanshu.state.engine import StateEngine
from xuanshu.strategies.signals import build_candidate_signals


def test_trader_generates_breakout_signal_for_trend_expansion() -> None:
    engine = StateEngine()
    engine.on_bbo("BTC-USDT-SWAP", bid=100.0, ask=100.2)
    engine.on_trade("BTC-USDT-SWAP", price=100.3, size=5.0, side="buy")
    engine.on_trade("BTC-USDT-SWAP", price=100.4, size=4.0, side="buy")

    snapshot = engine.snapshot("BTC-USDT-SWAP")
    signals = build_candidate_signals(snapshot)

    assert snapshot.regime == "trend_expansion"
    assert signals[0].strategy_id == "breakout"

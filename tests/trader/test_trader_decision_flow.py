from datetime import UTC, datetime

import pytest

import xuanshu.apps.trader as trader_app
from xuanshu.contracts.strategy import ApprovedStrategyBinding
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


def test_trader_empty_cold_start_snapshot_stays_unknown() -> None:
    engine = StateEngine()

    snapshot = engine.snapshot("BTC-USDT-SWAP")
    signals = build_candidate_signals(snapshot)

    assert snapshot.mid_price == 0.0
    assert snapshot.spread == 0.0
    assert snapshot.recent_trade_bias == 0.0
    assert snapshot.regime == MarketRegime.UNKNOWN
    assert signals[0].strategy_id == StrategyId.RISK_PAUSE


def test_trader_recent_flow_overrides_stale_lifetime_pressure() -> None:
    engine = StateEngine(recent_trade_window=6)
    engine.on_bbo("BTC-USDT-SWAP", bid=100.0, ask=100.2)
    engine.on_trade("BTC-USDT-SWAP", price=99.0, size=1_000.0, side="sell")
    for _ in range(5):
        engine.on_trade("BTC-USDT-SWAP", price=100.3, size=1.0, side="buy")

    snapshot = engine.snapshot("BTC-USDT-SWAP")

    assert snapshot.recent_trade_bias > 0.6
    assert snapshot.regime == MarketRegime.TREND


def test_trader_rejects_unsupported_trade_side() -> None:
    engine = StateEngine()

    with pytest.raises(ValueError, match="unsupported trade side"):
        engine.on_trade("BTC-USDT-SWAP", price=100.0, size=1.0, side="hold")


def _build_binding(
    *,
    score: float = 100.0,
    score_basis: str = "backtest_return_percent",
) -> ApprovedStrategyBinding:
    now = datetime.now(UTC)
    return ApprovedStrategyBinding(
        strategy_def_id="strat-1",
        strategy_package_id="pkg-1",
        backtest_report_id="bt-1",
        score=score,
        score_basis=score_basis,
        approval_record_id="apr-1",
        activated_at=now,
    )


def test_trader_replacement_helper_requires_at_least_ten_percent_improvement() -> None:
    current = _build_binding(score=100.0)
    candidate = _build_binding(score=110.0)
    near_miss = _build_binding(score=109.99)

    assert trader_app._is_stronger_replacement(current, candidate) is True
    assert trader_app._is_stronger_replacement(current, near_miss) is False


def test_trader_replacement_helper_accepts_candidate_when_current_is_missing() -> None:
    candidate = _build_binding(score=55.0)

    assert trader_app._is_stronger_replacement(None, candidate) is True


def test_trader_replacement_helper_fails_closed_on_score_basis_mismatch_and_invalid_score() -> None:
    current = _build_binding(score=100.0)
    mismatched_basis = ApprovedStrategyBinding.model_construct(
        strategy_def_id="strat-1",
        strategy_package_id="pkg-1",
        backtest_report_id="bt-1",
        score=120.0,
        score_basis="sharpe_ratio",
        approval_record_id="apr-1",
        activated_at=datetime.now(UTC),
    )
    invalid_score = ApprovedStrategyBinding.model_construct(
        strategy_def_id="strat-1",
        strategy_package_id="pkg-1",
        backtest_report_id="bt-1",
        score=float("nan"),
        score_basis="backtest_return_percent",
        approval_record_id="apr-1",
        activated_at=datetime.now(UTC),
    )

    assert trader_app._is_stronger_replacement(current, mismatched_basis) is False
    assert trader_app._is_stronger_replacement(current, invalid_score) is False


def test_trader_build_strategy_handover_events_respects_required_order() -> None:
    current = _build_binding(score=100.0)
    candidate = _build_binding(score=112.0)

    events = trader_app._build_strategy_handover_events("BTC-USDT-SWAP", current, candidate)

    assert [event["event_type"] for event in events] == [
        "cancel_open_orders",
        "flatten_position",
        "mark_replaced",
        "activate_new_strategy",
    ]


def test_trader_runtime_defaults_track_active_symbol_strategies_and_handover_state(monkeypatch) -> None:
    monkeypatch.setenv("XUANSHU_OKX_SYMBOLS", "BTC-USDT-SWAP")
    monkeypatch.setenv("XUANSHU_TRADER_STARTING_NAV", "250000")
    monkeypatch.setenv("OKX_API_KEY", "api-key")
    monkeypatch.setenv("OKX_API_SECRET", "api-secret")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "api-passphrase")

    runtime = trader_app.build_trader_runtime()

    assert runtime.active_symbol_strategies == {}
    assert runtime.symbol_handover_state == {}

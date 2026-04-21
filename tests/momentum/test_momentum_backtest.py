from datetime import UTC, datetime, timedelta

from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.momentum.backtest import (
    MomentumBacktestConfig,
    MomentumParameterSet,
    build_momentum_snapshot,
    evaluate_momentum_candidate,
    select_best_candidate,
)


def _rows(closes: list[float]) -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        {
            "timestamp": start + timedelta(hours=index),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
        }
        for index, close in enumerate(closes)
    ]


def test_evaluate_momentum_candidate_trades_positive_breakouts() -> None:
    params = MomentumParameterSet(
        lookback=3,
        stop_loss_bps=100,
        take_profit_bps=200,
        max_hold_minutes=180,
    )

    result = evaluate_momentum_candidate(params, _rows([100, 101, 102, 104, 107, 109, 112, 114, 117]))

    assert result.trade_count >= 1
    assert result.return_percent > 0
    assert result.profit_factor > 1


def test_select_best_candidate_rejects_when_gates_fail() -> None:
    config = MomentumBacktestConfig(
        min_trade_count=30,
        max_drawdown_percent=5.0,
        risk_fraction=0.25,
    )
    bad_result = evaluate_momentum_candidate(
        MomentumParameterSet(lookback=3, stop_loss_bps=100, take_profit_bps=200, max_hold_minutes=180),
        _rows([100, 99, 98, 97, 96, 95, 94]),
    )

    selected = select_best_candidate([bad_result], config=config)

    assert selected is None


def test_build_momentum_snapshot_serializes_single_fixed_strategy() -> None:
    config = MomentumBacktestConfig(
        min_trade_count=1,
        max_drawdown_percent=20.0,
        risk_fraction=0.25,
    )
    result = evaluate_momentum_candidate(
        MomentumParameterSet(lookback=3, stop_loss_bps=100, take_profit_bps=200, max_hold_minutes=180),
        _rows([100, 101, 102, 104, 107, 109, 112, 114, 117]),
    )

    snapshot = build_momentum_snapshot(
        selected=result,
        symbol="BTC-USDT-SWAP",
        generated_at=datetime(2026, 1, 2, tzinfo=UTC),
        config=config,
    )

    assert snapshot.symbol_whitelist == ["BTC-USDT-SWAP"]
    assert snapshot.market_mode == RunMode.HALTED
    assert snapshot.approval_state == ApprovalState.APPROVED
    assert snapshot.strategy_enable_flags == {"momentum": True}
    assert snapshot.symbol_strategy_bindings["BTC-USDT-SWAP"].strategy_def_id.startswith("momentum-btc-usdt-swap-")

from datetime import UTC, datetime, timedelta

from xuanshu.core.enums import ApprovalState, RunMode
from xuanshu.vol_breakout.backtest import (
    VolBreakoutConfig,
    VolBreakoutParameters,
    build_vol_breakout_snapshot,
    evaluate_vol_breakout,
)


def _rows(closes: list[float]) -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        {
            "timestamp": start + timedelta(hours=4 * index),
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
        }
        for index, close in enumerate(closes)
    ]


def test_vol_breakout_enters_on_atr_expansion_and_exits_on_trailing_stop() -> None:
    rows = _rows([100 + index * 0.1 for index in range(210)] + [125, 130, 136, 134, 131, 127])
    params = VolBreakoutParameters(k=0.8, trailing_atr=2.5, max_hold_bars=12)

    result = evaluate_vol_breakout(params, rows, config=VolBreakoutConfig(min_trade_count=1))

    assert result.trade_count == 1
    assert result.return_percent > 0
    assert result.profit_factor == 999.0


def test_build_vol_breakout_snapshot_serializes_halted_eth_strategy() -> None:
    rows = _rows([100 + index * 0.1 for index in range(210)] + [125, 130, 136, 134, 131, 127])
    params = VolBreakoutParameters(k=0.8, trailing_atr=2.5, max_hold_bars=12)
    config = VolBreakoutConfig(min_trade_count=1, risk_fraction=0.25)
    result = evaluate_vol_breakout(params, rows, config=config)

    snapshot = build_vol_breakout_snapshot(
        selected=result,
        symbol="ETH-USDT-SWAP",
        generated_at=datetime(2026, 1, 2, tzinfo=UTC),
        config=config,
    )

    assert snapshot.symbol_whitelist == ["ETH-USDT-SWAP"]
    assert snapshot.strategy_enable_flags == {"vol_breakout": True}
    assert snapshot.market_mode == RunMode.HALTED
    assert snapshot.approval_state == ApprovalState.APPROVED
    assert snapshot.symbol_strategy_bindings["ETH-USDT-SWAP"].strategy_def_id.startswith(
        "vol-breakout-eth-usdt-swap-4h-"
    )


def test_vol_breakout_backtest_blocks_when_live_sizing_cannot_open_minimum_contract() -> None:
    rows = _rows([100 + index * 0.1 for index in range(210)] + [125, 130, 136, 134, 131, 127])
    params = VolBreakoutParameters(k=0.8, trailing_atr=2.5, max_hold_bars=12)
    config = VolBreakoutConfig(
        min_trade_count=1,
        symbol="ETH-USDT-SWAP",
        initial_equity=667.0,
        initial_available_balance=1.0,
    )

    result = evaluate_vol_breakout(params, rows, config=config)

    assert result.trade_count == 0
    assert result.blocked_signal_count == 3

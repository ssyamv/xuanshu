from datetime import UTC, datetime, timedelta

from xuanshu.vote_trend.backtest import VoteTrendConfig, VoteTrendParameters, evaluate_vote_trend


def _trend_rows() -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    closes = [100 + index * 0.1 for index in range(220)]
    closes.extend([130, 135, 142, 150, 160, 171, 184, 198])
    return [
        {
            "timestamp": start + timedelta(hours=12 * index),
            "open": close - 1.0,
            "high": close + 2.0,
            "low": close - 2.0,
            "close": close,
        }
        for index, close in enumerate(closes)
    ]


def test_vote_trend_opens_long_on_multi_factor_trend_vote() -> None:
    result = evaluate_vote_trend(
        VoteTrendParameters(
            fast_ema_period=20,
            slow_ema_period=50,
            lookback_bars=3,
            channel_bars=6,
            threshold_bps=0,
            required_votes=4,
            stop_loss_bps=200,
            take_profit_bps=1000,
            max_hold_bars=6,
        ),
        _trend_rows(),
        config=VoteTrendConfig(symbol="BTC-USDT-SWAP"),
    )

    assert result.trade_count >= 1
    assert result.long_trade_count >= 1
    assert result.return_percent > 0


def test_vote_trend_can_disable_short_entries() -> None:
    rows = _trend_rows()
    parameters = VoteTrendParameters(
        fast_ema_period=20,
        slow_ema_period=50,
        lookback_bars=3,
        channel_bars=6,
        threshold_bps=0,
        required_votes=4,
        stop_loss_bps=200,
        take_profit_bps=1000,
        max_hold_bars=6,
        allow_short=False,
    )

    result = evaluate_vote_trend(parameters, rows, config=VoteTrendConfig(symbol="BTC-USDT-SWAP"))

    assert result.short_trade_count == 0


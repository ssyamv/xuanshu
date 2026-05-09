from datetime import UTC, datetime, timedelta

import pytest

import xuanshu.apps.vote_trend_backtest as app


def _rows() -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    closes = [100 + index * 0.1 for index in range(220)] + [130, 135, 142, 150, 160, 171, 184, 198]
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


@pytest.mark.asyncio
async def test_run_vote_trend_backtest_prints_summary(monkeypatch, capsys) -> None:
    async def fake_fetch(*args, **kwargs):
        return _rows()

    monkeypatch.setattr(app, "fetch_okx_history_rows", fake_fetch)

    exit_code = await app.run_backtest(
        [
            "--slow-ema-period",
            "50",
            "--lookback-bars",
            "3",
            "--channel-bars",
            "6",
            "--max-hold-bars",
            "6",
        ]
    )

    assert exit_code == 0
    assert "vote_trend symbol=BTC-USDT-SWAP" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_vote_trend_backtest_writes_snapshot(monkeypatch, tmp_path) -> None:
    async def fake_fetch(*args, **kwargs):
        return _rows()

    output_path = tmp_path / "active_strategy.json"
    monkeypatch.setattr(app, "fetch_okx_history_rows", fake_fetch)

    exit_code = await app.run_backtest(
        [
            "--slow-ema-period",
            "50",
            "--lookback-bars",
            "3",
            "--channel-bars",
            "6",
            "--max-hold-bars",
            "6",
            "--output",
            str(output_path),
            "--activate-normal",
        ]
    )

    assert exit_code == 0
    payload = output_path.read_text(encoding="utf-8")
    assert '"strategy_enable_flags": {' in payload
    assert '"vote_trend": true' in payload
    assert '"market_mode": "normal"' in payload

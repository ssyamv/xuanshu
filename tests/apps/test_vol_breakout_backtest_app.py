import json
from datetime import UTC, datetime, timedelta

import pytest

import xuanshu.apps.vol_breakout_backtest as app


def _rows() -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    closes = [100 + index * 0.1 for index in range(210)] + [125, 130, 136, 134, 131, 127]
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


@pytest.mark.asyncio
async def test_run_backtest_writes_eth_vol_breakout_snapshot(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "active_strategy.json"

    async def fake_fetch(*args, **kwargs):
        return _rows()

    monkeypatch.setattr(app, "fetch_okx_history_rows", fake_fetch)

    exit_code = await app.run_backtest(["--output", str(output_path), "--min-trades", "1"])

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["symbol_whitelist"] == ["ETH-USDT-SWAP"]
    assert payload["strategy_enable_flags"] == {"vol_breakout": True}
    assert payload["market_mode"] == "halted"


@pytest.mark.asyncio
async def test_run_backtest_accepts_atr_and_ema_period_overrides(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "active_strategy.json"

    async def fake_fetch(*args, **kwargs):
        return _rows()

    monkeypatch.setattr(app, "fetch_okx_history_rows", fake_fetch)

    exit_code = await app.run_backtest(
        [
            "--symbol",
            "BTC-USDT-SWAP",
            "--bar",
            "1D",
            "--output",
            str(output_path),
            "--min-trades",
            "1",
            "--atr-period",
            "7",
            "--ema-period",
            "20",
        ]
    )

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    binding = payload["symbol_strategy_bindings"]["BTC-USDT-SWAP"]
    assert payload["source_reason"] == "fixed BTC-USDT-SWAP 1D volatility breakout backtest"
    assert "1d" in binding["strategy_def_id"]

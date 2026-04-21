import json
from datetime import UTC, datetime, timedelta

import pytest

import xuanshu.apps.momentum_backtest as app


def _rows() -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    closes = [100 + index * 2 for index in range(80)]
    return [
        {"timestamp": start + timedelta(hours=index), "open": close, "high": close + 1, "low": close - 1, "close": close}
        for index, close in enumerate(closes)
    ]


@pytest.mark.asyncio
async def test_run_backtest_writes_fixed_snapshot(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "active_strategy.json"

    async def fake_fetch(*args, **kwargs):
        return _rows()

    monkeypatch.setattr(app, "fetch_okx_history_rows", fake_fetch)

    exit_code = await app.run_backtest(["--output", str(output_path), "--limit", "80", "--min-trades", "1"])

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["symbol_whitelist"] == ["BTC-USDT-SWAP"]
    assert payload["strategy_enable_flags"] == {"momentum": True}


@pytest.mark.asyncio
async def test_run_backtest_does_not_write_when_no_candidate_passes(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "active_strategy.json"

    async def fake_fetch(*args, **kwargs):
        return _rows()[:10]

    monkeypatch.setattr(app, "fetch_okx_history_rows", fake_fetch)

    exit_code = await app.run_backtest(["--output", str(output_path), "--limit", "10", "--min-trades", "30"])

    assert exit_code == 2
    assert not output_path.exists()

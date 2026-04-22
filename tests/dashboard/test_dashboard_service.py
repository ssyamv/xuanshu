from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from xuanshu.dashboard.app import create_app
from xuanshu.dashboard.service import DashboardService


@dataclass
class _FakeRuntimeReader:
    budget: dict[str, object] = field(default_factory=dict)
    summaries: dict[str, dict[str, object]] = field(default_factory=dict)
    mode: str = "normal"
    healthy: bool = True

    def get_run_mode(self) -> object | None:
        return self.mode

    def get_budget_pool_summary(self) -> dict[str, object] | None:
        return self.budget

    def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
        return self.summaries.get(symbol)

    def ping(self) -> bool:
        return self.healthy


@dataclass
class _FakeHistoryReader:
    rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    healthy: bool = True

    def list_recent_rows(self, table: str, limit: int = 10) -> list[dict[str, Any]]:
        return list(reversed(self.rows.get(table, [])))[0:limit]

    def list_rows_since(self, table: str, *, since: datetime | None, limit: int) -> list[dict[str, Any]]:
        rows = self.rows.get(table, [])
        if since is not None:
            rows = [row for row in rows if datetime.fromisoformat(str(row["created_at"])) >= since]
        return rows[:limit]

    def ping(self) -> bool:
        return self.healthy


def _service(runtime: _FakeRuntimeReader, history: _FakeHistoryReader) -> DashboardService:
    return DashboardService(
        runtime_reader=runtime,
        history_reader=history,
        symbols=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
        app_version="test",
    )


def test_overview_uses_redis_budget_and_enriches_current_positions() -> None:
    runtime = _FakeRuntimeReader(
        budget={"equity": 1_025.5, "strategy_total_amount": 1_000.0, "available_balance": 88.0},
        summaries={"BTC-USDT-SWAP": {"net_quantity": 2, "mid_price": 70_000, "regime": "trend"}},
    )
    history = _FakeHistoryReader(
        rows={
            "positions": [
                {
                    "created_at": "2026-04-21T00:00:00+00:00",
                    "symbol": "BTC-USDT-SWAP",
                    "net_quantity": 2,
                    "position_side": "long",
                    "average_price": 69_500,
                    "mark_price": 70_100,
                    "unrealized_pnl": 12.25,
                    "strategy_id": "vol_breakout",
                    "strategy_logic": "突破",
                }
            ]
        }
    )

    overview = _service(runtime, history).overview()

    assert overview["equity"] == 1025.5
    assert overview["estimated_pnl"] == 25.5
    assert overview["estimated_pnl_rate"] == 0.0255
    assert overview["positions"][0]["symbol"] == "BTC-USDT-SWAP"
    assert overview["positions"][0]["strategy_id"] == "vol_breakout"


def test_equity_curve_samples_checkpoints_and_appends_live_equity() -> None:
    now = datetime.now(UTC)
    runtime = _FakeRuntimeReader(budget={"equity": 1_030.0, "strategy_total_amount": 1_000.0})
    history = _FakeHistoryReader(
        rows={
            "execution_checkpoints": [
                {
                    "created_at": (now - timedelta(minutes=10)).isoformat(),
                    "positions_snapshot": [{"symbol": "BTC-USDT-SWAP", "unrealized_pnl": 10.0}],
                },
                {
                    "created_at": (now - timedelta(minutes=5)).isoformat(),
                    "positions_snapshot": [{"symbol": "BTC-USDT-SWAP", "unrealized_pnl": 20.0}],
                },
            ]
        }
    )

    curve = _service(runtime, history).equity_curve("24h")

    assert curve["range"] == "24h"
    assert curve["points"][0]["equity"] == 1010.0
    assert curve["points"][1]["pnl_rate"] == 0.02
    assert curve["points"][-1]["equity"] == 1030.0


def test_actions_map_orders_fills_and_position_transitions() -> None:
    runtime = _FakeRuntimeReader()
    history = _FakeHistoryReader(
        rows={
            "orders": [
                {
                    "created_at": "2026-04-21T00:00:01+00:00",
                    "symbol": "BTC-USDT-SWAP",
                    "status": "submitted",
                    "side": "buy",
                    "client_order_id": "cid-1",
                    "strategy_id": "vol_breakout",
                },
                {
                    "created_at": "2026-04-21T00:00:02+00:00",
                    "symbol": "BTC-USDT-SWAP",
                    "status": "canceled",
                    "side": "buy",
                    "client_order_id": "cid-1",
                },
            ],
            "fills": [
                {
                    "created_at": "2026-04-21T00:00:03+00:00",
                    "symbol": "ETH-USDT-SWAP",
                    "side": "sell",
                    "filled_size": 1.5,
                    "fill_price": 2300,
                }
            ],
            "positions": [
                {
                    "created_at": "2026-04-21T00:00:04+00:00",
                    "symbol": "ETH-USDT-SWAP",
                    "net_quantity": 1.5,
                    "average_price": 2300,
                },
                {
                    "created_at": "2026-04-21T00:00:05+00:00",
                    "symbol": "ETH-USDT-SWAP",
                    "net_quantity": 0,
                    "mark_price": 2310,
                },
            ],
        }
    )

    labels = [item["label"] for item in _service(runtime, history).actions(limit=10)]

    assert "下单" in labels
    assert "撤单" in labels
    assert "成交" in labels
    assert "开仓" in labels
    assert "平仓" in labels


def test_dashboard_api_shapes() -> None:
    runtime = _FakeRuntimeReader(budget={"equity": 1000, "strategy_total_amount": 1000})
    history = _FakeHistoryReader()
    client = TestClient(create_app(_service(runtime, history)))

    assert client.get("/xuanshu/healthz").json()["read_only"] is True
    assert "positions" in client.get("/xuanshu/api/overview").json()
    assert "points" in client.get("/xuanshu/api/equity-curve?range=24h").json()
    assert "actions" in client.get("/xuanshu/api/actions?limit=5").json()

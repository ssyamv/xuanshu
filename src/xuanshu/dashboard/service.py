from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from xuanshu.infra.storage.postgres_store import POSTGRES_TABLES, PostgresRuntimeStore
from xuanshu.infra.storage.redis_store import RedisKeys, RedisRuntimeStateStore

ActionRow = dict[str, Any]
PayloadRow = dict[str, Any]

_RANGE_DELTAS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
_ORDER_ACTION_LABELS = {
    "submitted": "下单",
    "live": "挂单",
    "filled": "成交",
    "canceled": "撤单",
    "cancelled": "撤单",
    "rejected": "拒单",
}


class RuntimeReader(Protocol):
    def get_run_mode(self) -> object | None:
        ...

    def get_budget_pool_summary(self) -> dict[str, object] | None:
        ...

    def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
        ...

    def ping(self) -> bool:
        ...


class HistoryReader(Protocol):
    def list_recent_rows(self, table: str, limit: int = 10) -> list[PayloadRow]:
        ...

    def list_rows_since(self, table: str, *, since: datetime | None, limit: int) -> list[PayloadRow]:
        ...

    def ping(self) -> bool:
        ...


@dataclass(slots=True)
class DashboardService:
    runtime_reader: RuntimeReader
    history_reader: HistoryReader
    symbols: tuple[str, ...]
    app_version: str

    def overview(self) -> dict[str, object]:
        budget = self.runtime_reader.get_budget_pool_summary() or {}
        positions = self.current_positions()
        latest_equity = _positive_float(budget.get("equity"))
        base_equity = _base_equity(budget)
        if latest_equity is None:
            latest_equity = _estimate_equity_from_positions(base_equity, positions)
        pnl = latest_equity - base_equity
        pnl_rate = pnl / base_equity if base_equity > 0 else 0.0
        mode = self.runtime_reader.get_run_mode()
        mode_value = getattr(mode, "value", mode) or budget.get("current_mode") or "unknown"
        return {
            "mode": str(mode_value),
            "equity": _round_money(latest_equity),
            "base_equity": _round_money(base_equity),
            "estimated_pnl": _round_money(pnl),
            "estimated_pnl_rate": round(pnl_rate, 6),
            "available_balance": _round_money(_positive_float(budget.get("available_balance")) or 0.0),
            "margin_ratio": _round_money(_positive_float(budget.get("margin_ratio")) or 0.0),
            "strategy_total_amount": _round_money(_positive_float(budget.get("strategy_total_amount")) or base_equity),
            "starting_nav": _round_money(_positive_float(budget.get("starting_nav")) or base_equity),
            "last_public_stream_marker": budget.get("last_public_stream_marker"),
            "last_private_stream_marker": budget.get("last_private_stream_marker"),
            "positions": positions,
            "actions": self.actions(limit=12),
            "as_of": datetime.now(UTC).isoformat(),
            "equity_is_estimated": True,
        }

    def equity_curve(self, range_key: str = "24h") -> dict[str, object]:
        normalized_range = range_key if range_key in {*_RANGE_DELTAS, "all"} else "24h"
        since = None if normalized_range == "all" else datetime.now(UTC) - _RANGE_DELTAS[normalized_range]
        budget = self.runtime_reader.get_budget_pool_summary() or {}
        base_equity = _base_equity(budget)
        rows = self.history_reader.list_rows_since("execution_checkpoints", since=since, limit=5_000)
        points = _sample_points([_checkpoint_point(row, base_equity) for row in rows if row], max_points=240)
        live_equity = _positive_float(budget.get("equity"))
        if live_equity is not None:
            live_point = {
                "timestamp": datetime.now(UTC).isoformat(),
                "equity": _round_money(live_equity),
                "pnl": _round_money(live_equity - base_equity),
                "pnl_rate": round((live_equity - base_equity) / base_equity, 6) if base_equity > 0 else 0.0,
            }
            if not points or points[-1]["timestamp"] != live_point["timestamp"]:
                points.append(live_point)
        return {
            "range": normalized_range,
            "base_equity": _round_money(base_equity),
            "points": points,
            "equity_is_estimated": True,
        }

    def actions(self, limit: int = 100) -> list[ActionRow]:
        bounded_limit = max(1, min(limit, 500))
        actions: list[ActionRow] = []
        actions.extend(self._order_actions(limit=bounded_limit * 2))
        actions.extend(self._fill_actions(limit=bounded_limit))
        actions.extend(self._position_transition_actions(limit=bounded_limit * 5))
        actions.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
        return actions[:bounded_limit]

    def current_positions(self) -> list[dict[str, object]]:
        recent_positions = self.history_reader.list_recent_rows("positions", limit=max(50, len(self.symbols) * 20))
        latest_by_symbol: dict[str, PayloadRow] = {}
        for row in recent_positions:
            symbol = str(row.get("symbol") or "")
            if symbol and symbol not in latest_by_symbol:
                latest_by_symbol[symbol] = row

        positions: list[dict[str, object]] = []
        for symbol in self.symbols:
            runtime = self.runtime_reader.get_symbol_runtime_summary(symbol) or {}
            latest = latest_by_symbol.get(symbol, {})
            net_quantity = _first_number(runtime.get("net_quantity"), latest.get("net_quantity"))
            if net_quantity == 0.0 and not runtime and not latest:
                continue
            positions.append(
                {
                    "symbol": symbol,
                    "side": str(latest.get("position_side") or runtime.get("position_side") or "n/a"),
                    "net_quantity": net_quantity,
                    "average_price": _round_money(_positive_or_zero(latest.get("average_price"))),
                    "mark_price": _round_money(
                        _positive_or_zero(latest.get("mark_price"), runtime.get("mid_price"))
                    ),
                    "unrealized_pnl": _round_money(_number_or_zero(latest.get("unrealized_pnl"))),
                    "mid_price": _round_money(_positive_or_zero(runtime.get("mid_price"))),
                    "regime": runtime.get("regime", "unknown"),
                    "open_order_count": int(_number_or_zero(runtime.get("open_order_count"))),
                    "strategy_id": latest.get("strategy_id") or "未提供",
                    "strategy_logic": latest.get("strategy_logic") or "未提供",
                }
            )
        return positions

    def health(self) -> dict[str, object]:
        return {
            "status": "ok" if self.runtime_reader.ping() and self.history_reader.ping() else "degraded",
            "redis": self.runtime_reader.ping(),
            "postgres": self.history_reader.ping(),
            "version": self.app_version,
            "read_only": True,
        }

    def _order_actions(self, *, limit: int) -> list[ActionRow]:
        rows = self.history_reader.list_recent_rows("orders", limit=limit)
        actions: list[ActionRow] = []
        for row in rows:
            status = str(row.get("status") or "").lower()
            label = _ORDER_ACTION_LABELS.get(status)
            if label is None:
                continue
            actions.append(
                _action(
                    row,
                    action_type=f"order_{status}",
                    label=label,
                    size=row.get("filled_size", row.get("size")),
                    price=row.get("price"),
                )
            )
        return actions

    def _fill_actions(self, *, limit: int) -> list[ActionRow]:
        rows = self.history_reader.list_recent_rows("fills", limit=limit)
        return [
            _action(
                row,
                action_type="fill",
                label="成交",
                size=row.get("filled_size", row.get("size")),
                price=row.get("fill_price", row.get("price")),
            )
            for row in rows
        ]

    def _position_transition_actions(self, *, limit: int) -> list[ActionRow]:
        rows = list(reversed(self.history_reader.list_recent_rows("positions", limit=limit)))
        previous_by_symbol: dict[str, float] = {}
        actions: list[ActionRow] = []
        for row in rows:
            symbol = str(row.get("symbol") or "unknown")
            quantity = _number_or_zero(row.get("net_quantity", row.get("quantity")))
            previous = previous_by_symbol.get(symbol, 0.0)
            previous_by_symbol[symbol] = quantity
            if previous == 0.0 and quantity != 0.0:
                actions.append(
                    _action(row, action_type="position_opened", label="开仓", size=quantity, price=row.get("average_price"))
                )
            elif previous != 0.0 and quantity == 0.0:
                actions.append(
                    _action(row, action_type="position_closed", label="平仓", size=previous, price=row.get("mark_price"))
                )
        return actions


class RedisDashboardReader:
    def __init__(self, redis_url: str) -> None:
        self._client = Redis.from_url(redis_url)
        self._store = RedisRuntimeStateStore(redis_client=self._client)

    def get_run_mode(self) -> object | None:
        return self._store.get_run_mode()

    def get_budget_pool_summary(self) -> dict[str, object] | None:
        return self._store.get_budget_pool_summary()

    def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
        return self._store.get_symbol_runtime_summary(symbol)

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except RedisError:
            return False


class PostgresDashboardReader:
    def __init__(self, dsn: str) -> None:
        self._store = PostgresRuntimeStore(dsn=dsn)
        self._engine: Engine = create_engine(dsn, future=True)

    def list_recent_rows(self, table: str, limit: int = 10) -> list[PayloadRow]:
        return self._store.list_recent_rows(table, limit=limit)

    def list_rows_since(self, table: str, *, since: datetime | None, limit: int) -> list[PayloadRow]:
        if table not in POSTGRES_TABLES:
            raise ValueError(f"unknown table: {table}")
        bounded_limit = max(1, min(limit, 10_000))
        query = f"select payload, created_at from {table}"
        params: dict[str, object] = {"limit": bounded_limit}
        if since is not None:
            query += " where created_at >= :since"
            params["since"] = since
        query += " order by id asc limit :limit"
        try:
            with self._engine.begin() as connection:
                rows = connection.execute(text(query), params).all()
        except SQLAlchemyError:
            return []
        return [_hydrate_payload(row.payload, row.created_at) for row in rows if isinstance(row.payload, dict)]

    def ping(self) -> bool:
        try:
            with self._engine.begin() as connection:
                connection.execute(select(1)).scalar_one()
        except SQLAlchemyError:
            return False
        return True


def _hydrate_payload(payload: Any, created_at: Any) -> PayloadRow:
    row = dict(payload)
    row.setdefault("created_at", _timestamp(created_at))
    return row


def _checkpoint_point(row: PayloadRow, base_equity: float) -> dict[str, object]:
    positions = row.get("positions_snapshot")
    unrealized = 0.0
    if isinstance(positions, list):
        unrealized = sum(_number_or_zero(item.get("unrealized_pnl")) for item in positions if isinstance(item, dict))
    equity = base_equity + unrealized
    timestamp = row.get("created_at") or row.get("timestamp")
    return {
        "timestamp": str(timestamp),
        "equity": _round_money(equity),
        "pnl": _round_money(equity - base_equity),
        "pnl_rate": round((equity - base_equity) / base_equity, 6) if base_equity > 0 else 0.0,
    }


def _sample_points(points: Iterable[dict[str, object]], *, max_points: int) -> list[dict[str, object]]:
    materialized = [point for point in points if point.get("timestamp")]
    if len(materialized) <= max_points:
        return materialized
    stride = max(1, len(materialized) // max_points)
    sampled = materialized[::stride]
    if sampled[-1] != materialized[-1]:
        sampled.append(materialized[-1])
    return sampled[:max_points]


def _action(row: PayloadRow, *, action_type: str, label: str, size: object, price: object) -> ActionRow:
    return {
        "timestamp": str(row.get("created_at") or row.get("timestamp") or ""),
        "type": action_type,
        "label": label,
        "symbol": row.get("symbol", "unknown"),
        "side": row.get("side", "n/a"),
        "size": _round_quantity(_number_or_zero(size)),
        "price": _round_money(_number_or_zero(price)),
        "status": row.get("status", ""),
        "order_id": row.get("order_id", ""),
        "client_order_id": row.get("client_order_id", ""),
        "strategy_id": row.get("strategy_id") or "未提供",
        "strategy_logic": row.get("strategy_logic") or "未提供",
    }


def _base_equity(budget: dict[str, object]) -> float:
    return (
        _positive_float(budget.get("strategy_total_amount"))
        or _positive_float(budget.get("starting_nav"))
        or 1.0
    )


def _estimate_equity_from_positions(base_equity: float, positions: list[dict[str, object]]) -> float:
    return base_equity + sum(_number_or_zero(position.get("unrealized_pnl")) for position in positions)


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _number_or_zero(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _positive_or_zero(*values: object) -> float:
    for value in values:
        parsed = _positive_float(value)
        if parsed is not None:
            return parsed
    return 0.0


def _first_number(*values: object) -> float:
    for value in values:
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _round_money(value: float) -> float:
    return round(float(value), 4)


def _round_quantity(value: float) -> float:
    return round(float(value), 8)


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value)

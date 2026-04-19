from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

POSTGRES_TABLES = (
    "orders",
    "fills",
    "positions",
    "risk_events",
    "strategy_snapshots",
    "execution_checkpoints",
    "expert_opinions",
    "governor_runs",
    "notification_events",
)


@dataclass
class PostgresRuntimeStore:
    dsn: str
    written_rows: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {table: [] for table in POSTGRES_TABLES}
    )

    def _append_row(self, table: str, payload: dict[str, Any]) -> None:
        self.written_rows[table].append(deepcopy(payload))

    def append_order_fact(self, payload: dict[str, Any]) -> None:
        self._append_row("orders", payload)

    def append_fill_fact(self, payload: dict[str, Any]) -> None:
        self._append_row("fills", payload)

    def append_position_fact(self, payload: dict[str, Any]) -> None:
        self._append_row("positions", payload)

    def append_risk_event(self, payload: dict[str, Any]) -> None:
        self._append_row("risk_events", payload)

    def save_checkpoint(self, payload: dict[str, Any]) -> None:
        self._append_row("execution_checkpoints", payload)

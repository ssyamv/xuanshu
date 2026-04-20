from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, Table, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

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
    "strategy_packages",
    "backtest_reports",
    "approval_records",
)


@dataclass
class PostgresRuntimeStore:
    dsn: str
    written_rows: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {table: [] for table in POSTGRES_TABLES}
    )
    _engine: Engine | None = field(init=False, default=None, repr=False)
    _metadata: MetaData | None = field(init=False, default=None, repr=False)
    _tables: dict[str, Table] = field(init=False, default_factory=dict, repr=False)
    _database_disabled: bool = field(init=False, default=False, repr=False)

    def _append_row(self, table: str, payload: dict[str, Any]) -> None:
        row = deepcopy(payload)
        self.written_rows[table].append(row)
        if not self._ensure_database():
            return
        assert self._engine is not None
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    self._tables[table].insert().values(payload=self._normalize_payload(row))
                )
        except SQLAlchemyError:
            self._disable_database()

    def append_order_fact(self, payload: dict[str, Any]) -> None:
        self._append_row("orders", payload)

    def append_fill_fact(self, payload: dict[str, Any]) -> None:
        self._append_row("fills", payload)

    def append_position_fact(self, payload: dict[str, Any]) -> None:
        self._append_row("positions", payload)

    def append_risk_event(self, payload: dict[str, Any]) -> None:
        self._append_row("risk_events", payload)

    def append_strategy_snapshot(self, payload: dict[str, Any]) -> None:
        self._append_row("strategy_snapshots", payload)

    def append_expert_opinion(self, payload: dict[str, Any]) -> None:
        self._append_row("expert_opinions", payload)

    def append_governor_run(self, payload: dict[str, Any]) -> None:
        self._append_row("governor_runs", payload)

    def save_checkpoint(self, payload: dict[str, Any]) -> None:
        self._append_row("execution_checkpoints", payload)

    def append_notification_event(self, payload: dict[str, Any]) -> None:
        self._append_row("notification_events", payload)

    def has_notification_event(self, *, dedupe_key: str, status: str | None = None) -> bool:
        if self._ensure_database():
            assert self._engine is not None
            payload_column = self._tables["notification_events"].c.payload
            query = select(self._tables["notification_events"].c.id).where(
                payload_column["dedupe_key"].as_string() == dedupe_key
            )
            if status is not None:
                query = query.where(payload_column["status"].as_string() == status)
            query = query.limit(1)
            try:
                with self._engine.begin() as connection:
                    return connection.execute(query).first() is not None
            except SQLAlchemyError:
                self._disable_database()
        for row in reversed(self.written_rows["notification_events"]):
            if row.get("dedupe_key") != dedupe_key:
                continue
            if status is not None and row.get("status") != status:
                continue
            return True
        return False

    def append_strategy_package(self, payload: dict[str, Any]) -> None:
        self._append_row("strategy_packages", payload)

    def append_backtest_report(self, payload: dict[str, Any]) -> None:
        self._append_row("backtest_reports", payload)

    def append_approval_record(self, payload: dict[str, Any]) -> None:
        self._append_row("approval_records", payload)

    def has_strategy_package(self, *, strategy_package_id: str) -> bool:
        return self._find_row_by_payload_fields(
            "strategy_packages",
            {"strategy_package_id": strategy_package_id},
        ) is not None

    def find_strategy_package(self, *, strategy_package_id: str) -> dict[str, Any] | None:
        return self._find_row_by_payload_fields(
            "strategy_packages",
            {"strategy_package_id": strategy_package_id},
        )

    def find_strategy_snapshot(self, *, version_id: str) -> dict[str, Any] | None:
        return self._find_row_by_payload_fields(
            "strategy_snapshots",
            {"version_id": version_id},
        )

    def has_backtest_report(self, *, backtest_report_id: str) -> bool:
        return self._find_row_by_payload_fields(
            "backtest_reports",
            {"backtest_report_id": backtest_report_id},
        ) is not None

    def find_approval_record(
        self,
        *,
        strategy_package_id: str,
        backtest_report_id: str,
    ) -> dict[str, Any] | None:
        return self._find_row_by_payload_fields(
            "approval_records",
            {
                "strategy_package_id": strategy_package_id,
                "backtest_report_id": backtest_report_id,
            },
        )

    def list_recent_rows(self, table: str, limit: int = 10) -> list[dict[str, Any]]:
        if table not in self.written_rows:
            raise ValueError(f"unknown table: {table}")
        if limit <= 0:
            return []
        if self._ensure_database():
            assert self._engine is not None
            query = (
                select(self._tables[table].c.payload, self._tables[table].c.created_at)
                .order_by(self._tables[table].c.id.desc())
                .limit(limit)
            )
            try:
                with self._engine.begin() as connection:
                    rows = connection.execute(query).all()
            except SQLAlchemyError:
                self._disable_database()
            else:
                hydrated_rows: list[dict[str, Any]] = []
                for row in rows:
                    payload = self._hydrate_row_payload(row.payload, row.created_at)
                    if payload is not None:
                        hydrated_rows.append(payload)
                return hydrated_rows
        return deepcopy(self.written_rows[table][-limit:][::-1])

    def _find_row_by_payload_fields(
        self,
        table: str,
        criteria: dict[str, str],
    ) -> dict[str, Any] | None:
        if table not in self.written_rows:
            raise ValueError(f"unknown table: {table}")
        if self._ensure_database():
            assert self._engine is not None
            payload_column = self._tables[table].c.payload
            query = select(self._tables[table].c.payload, self._tables[table].c.created_at)
            for key, value in criteria.items():
                query = query.where(payload_column[key].as_string() == value)
            query = query.order_by(self._tables[table].c.id.desc()).limit(1)
            try:
                with self._engine.begin() as connection:
                    row = connection.execute(query).first()
            except SQLAlchemyError:
                self._disable_database()
            else:
                if row is not None:
                    return self._hydrate_row_payload(row.payload, row.created_at)
        for row in reversed(self.written_rows[table]):
            if all(row.get(key) == value for key, value in criteria.items()):
                return deepcopy(row)
        return None

    def _ensure_database(self) -> bool:
        if self._database_disabled:
            return False
        if self._engine is not None:
            return True
        try:
            metadata = MetaData()
            tables = {
                table_name: Table(
                    table_name,
                    metadata,
                    Column("id", Integer, primary_key=True, autoincrement=True),
                    Column("payload", JSON, nullable=False),
                    Column(
                        "created_at",
                        DateTime(timezone=True),
                        nullable=False,
                        default=lambda: datetime.now(UTC),
                    ),
                )
                for table_name in POSTGRES_TABLES
            }
            engine = create_engine(self.dsn, future=True)
            metadata.create_all(engine)
        except (ModuleNotFoundError, SQLAlchemyError):
            self._database_disabled = True
            return False
        self._metadata = metadata
        self._tables = tables
        self._engine = engine
        return True

    def _disable_database(self) -> None:
        self._engine = None
        self._metadata = None
        self._tables = {}
        self._database_disabled = True

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(payload, default=self._json_default))

    def _hydrate_row_payload(self, payload: Any, created_at: Any) -> dict[str, Any] | None:
        row = deepcopy(payload)
        if not isinstance(row, dict):
            return None
        if "created_at" not in row:
            row["created_at"] = self._json_default(created_at)
        return row

    @staticmethod
    def _json_default(value: object) -> object:
        if isinstance(value, datetime | date):
            if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
                value = value.replace(tzinfo=UTC)
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json")
        if hasattr(value, "__dict__"):
            return dict(vars(value))
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

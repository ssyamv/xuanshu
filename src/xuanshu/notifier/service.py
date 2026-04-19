from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, Protocol

from xuanshu.core.enums import RunMode
from xuanshu.infra.notifier.telegram import TextMessagePayload, render_text_message


_MODE_LABELS: dict[RunMode, str] = {
    RunMode.NORMAL: "normal trading",
    RunMode.DEGRADED: "degraded trading",
    RunMode.REDUCE_ONLY: "reduce-only",
    RunMode.HALTED: "halted",
}
_RUN_MODE_PRIORITY: dict[RunMode, int] = {
    RunMode.NORMAL: 0,
    RunMode.DEGRADED: 1,
    RunMode.REDUCE_ONLY: 2,
    RunMode.HALTED: 3,
}
_RETRY_PRIORITY: dict[NotificationSeverity, int] = {
    "CRITICAL": 0,
    "WARN": 1,
    "INFO": 2,
}
_PROACTIVE_CATEGORY_PRIORITY: dict[str, int] = {
    "mode_change": 0,
    "snapshot_published": 1,
    "recovery_failed": 2,
    "risk_event": 3,
}


def format_mode_change(mode: RunMode) -> str:
    return f"Mode changed to {_MODE_LABELS[mode]}"


NotificationSeverity = Literal["INFO", "WARN", "CRITICAL"]


class RuntimeStateReader(Protocol):
    def get_run_mode(self) -> RunMode | None:
        ...

    def set_run_mode(self, mode: RunMode) -> None:
        ...

    def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
        ...

    def set_fault_flags(self, flags: dict[str, object]) -> None:
        ...

    def get_fault_flags(self) -> dict[str, object] | None:
        ...

    def get_budget_pool_summary(self) -> dict[str, object] | None:
        ...

    def get_governor_health_summary(self) -> dict[str, object] | None:
        ...


class SnapshotReader(Protocol):
    def get_latest_snapshot(self) -> object | None:
        ...


class NotificationHistoryStore(Protocol):
    def append_risk_event(self, payload: dict[str, object]) -> None:
        ...

    def append_notification_event(self, payload: dict[str, object]) -> None:
        ...

    def list_recent_rows(self, table: str, limit: int = 10) -> list[dict[str, object]]:
        ...


class NotifierService:
    def __init__(
        self,
        *,
        okx_symbols: tuple[str, ...],
        runtime_store: RuntimeStateReader,
        snapshot_store: SnapshotReader,
        history_store: NotificationHistoryStore,
    ) -> None:
        self._okx_symbols = okx_symbols
        self._runtime_store = runtime_store
        self._snapshot_store = snapshot_store
        self._history_store = history_store

    async def handle_command(self, text: str) -> TextMessagePayload:
        command = self._normalize_command(text)
        if command == "/status":
            return render_text_message(self._render_status())
        if command == "/mode":
            return render_text_message(self._render_mode())
        if command == "/market":
            return render_text_message(self._render_market())
        if command == "/positions":
            return render_text_message(self._render_positions())
        if command == "/orders":
            return render_text_message(self._render_orders())
        if command == "/risk":
            return render_text_message(self._render_risk())
        if command == "/takeover":
            return render_text_message(self._handle_takeover_command(text))
        return render_text_message(
            "Supported commands: /status /positions /orders /risk /mode /market /takeover"
        )

    async def flush_pending_notifications(self, *, adapter: object, limit: int = 20) -> int:
        pending = self._collect_pending_notifications(limit=limit)
        flushed = 0
        for row in pending:
            try:
                await self.deliver_text(
                    adapter=adapter,
                    text=str(row["text"]),
                    severity=row["severity"],
                    category=str(row["category"]),
                    dedupe_key=str(row["dedupe_key"]),
                )
            except Exception:
                continue
            flushed += 1
        return flushed

    async def flush_proactive_notifications(self, *, adapter: object, limit: int = 20) -> int:
        sent_notification_keys = self._collect_sent_notification_keys(limit=limit * 5)
        flushed = 0
        for candidate in self._collect_proactive_candidates(limit=limit):
            if candidate["dedupe_key"] in sent_notification_keys:
                continue
            await self.deliver_text(
                adapter=adapter,
                text=candidate["text"],
                severity=candidate["severity"],
                category=candidate["category"],
                dedupe_key=candidate["dedupe_key"],
            )
            flushed += 1
        return flushed

    async def deliver_text(
        self,
        *,
        adapter: object,
        text: str,
        severity: NotificationSeverity,
        category: str,
        dedupe_key: str,
    ) -> None:
        max_attempts = 3 if severity == "CRITICAL" else 1
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                await adapter.send_text(TextMessagePayload(text=text))
            except Exception as exc:
                last_error = exc
                continue
            self._history_store.append_notification_event(
                {
                    "category": category,
                    "dedupe_key": dedupe_key,
                    "severity": severity,
                    "status": "sent",
                    "attempt_count": attempt,
                    "needs_retry": False,
                    "text": text,
                }
            )
            return

        self._history_store.append_notification_event(
            {
                "category": category,
                "dedupe_key": dedupe_key,
                "severity": severity,
                "status": "failed",
                "attempt_count": max_attempts,
                "needs_retry": severity == "CRITICAL",
                "text": text,
            }
        )
        if last_error is not None:
            raise last_error

    def _normalize_command(self, text: str) -> str:
        command = text.strip().split(maxsplit=1)[0].lower()
        return command.split("@", 1)[0]

    def _handle_takeover_command(self, text: str) -> str:
        parts = text.strip().split(maxsplit=2)
        if len(parts) < 2:
            return "Usage: /takeover <degraded|reduce_only|halted> [reason]"
        requested_mode = parts[1].lower()
        if requested_mode not in {"degraded", "reduce_only", "halted"}:
            return "Usage: /takeover <degraded|reduce_only|halted> [reason]"
        reason = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else "operator requested"
        target_mode = RunMode(requested_mode)
        current_mode = self._runtime_store.get_run_mode() or RunMode.NORMAL
        effective_mode = max(current_mode, target_mode, key=lambda mode: _RUN_MODE_PRIORITY[mode])

        fault_flags = dict(self._runtime_store.get_fault_flags() or {})
        fault_flags["manual_takeover"] = {
            "requested_mode": effective_mode.value,
            "reason": reason,
        }

        self._runtime_store.set_run_mode(effective_mode)
        self._runtime_store.set_fault_flags(fault_flags)
        self._history_store.append_risk_event(
            {
                "event_type": "manual_takeover_requested",
                "symbol": "system",
                "detail": f"requested {effective_mode.value}: {reason}",
            }
        )
        return f"Manual takeover requested: {effective_mode.value} (reason={reason})"

    def _render_status(self) -> str:
        mode = self._render_mode()
        snapshot = self._snapshot_store.get_latest_snapshot()
        snapshot_version = getattr(snapshot, "version_id", "none")
        faults = self._runtime_store.get_fault_flags() or {}
        fault_labels = ", ".join(sorted(faults)) if faults else "none"
        lines = [mode, f"Snapshot: {snapshot_version}", f"Faults: {fault_labels}"]
        budget = self._runtime_store.get_budget_pool_summary()
        if isinstance(budget, dict):
            lines.append(
                "Budget: "
                f"remaining_notional={budget.get('remaining_notional', 'n/a')} "
                f"remaining_order_count={budget.get('remaining_order_count', 'n/a')}"
            )
        governor_health = self._runtime_store.get_governor_health_summary()
        if isinstance(governor_health, dict):
            lines.append(
                "Governor: "
                f"status={governor_health.get('status', 'unknown')} "
                f"trigger={governor_health.get('trigger', 'unknown')} "
                f"health={governor_health.get('health_state', 'unknown')}"
            )
        return "\n".join(lines)

    def _render_mode(self) -> str:
        mode = self._runtime_store.get_run_mode()
        return f"Mode: {mode.value if mode is not None else 'unknown'}"

    def _render_market(self) -> str:
        lines = list(self._render_symbol_summaries())
        if not lines:
            return "Market: no runtime summaries"
        return "\n".join(lines)

    def _render_positions(self) -> str:
        rows = self._history_store.list_recent_rows("positions", limit=5)
        if not rows:
            return "Positions: no recent position facts"
        return "\n".join(
            f"{row.get('symbol', 'unknown')}: "
            f"net={row.get('net_quantity', row.get('quantity', 'n/a'))} "
            f"avg={row.get('average_price', 'n/a')} "
            f"upnl={row.get('unrealized_pnl', 'n/a')}"
            for row in rows
        )

    def _render_orders(self) -> str:
        rows = self._history_store.list_recent_rows("orders", limit=5)
        if not rows:
            return "Orders: no recent orders"
        return "\n".join(
            f"{row.get('symbol', 'unknown')} "
            f"{row.get('side', 'n/a')} "
            f"{row.get('status', 'n/a')} "
            f"cid={row.get('client_order_id', row.get('order_id', 'n/a'))}"
            for row in rows
        )

    def _render_risk(self) -> str:
        rows = self._history_store.list_recent_rows("risk_events", limit=5)
        lines = []
        if rows:
            lines.extend(
                f"{row.get('symbol', 'system')}: {row.get('event_type', row.get('reason', 'risk_event'))}"
                + (f" {row.get('detail')}" if row.get("detail") else "")
                for row in rows
            )
        else:
            lines.append("Risk: no recent risk events")
        budget = self._runtime_store.get_budget_pool_summary()
        if isinstance(budget, dict):
            lines.append(
                "Budget: "
                f"remaining_notional={budget.get('remaining_notional', 'n/a')} "
                f"remaining_order_count={budget.get('remaining_order_count', 'n/a')} "
                f"current_mode={budget.get('current_mode', 'n/a')}"
            )
        governor_health = self._runtime_store.get_governor_health_summary()
        if isinstance(governor_health, dict):
            lines.append(
                "Governor: "
                f"status={governor_health.get('status', 'unknown')} "
                f"trigger={governor_health.get('trigger', 'unknown')} "
                f"health={governor_health.get('health_state', 'unknown')}"
            )
        return "\n".join(lines)

    def _render_symbol_summaries(self) -> Iterable[str]:
        for symbol in self._okx_symbols:
            summary = self._runtime_store.get_symbol_runtime_summary(symbol)
            if summary is None:
                continue
            mid_price = summary.get("mid_price", "n/a")
            net_quantity = summary.get("net_quantity", "n/a")
            yield f"{symbol}: mid={mid_price} net={net_quantity}"

    def _collect_pending_notifications(self, *, limit: int) -> list[dict[str, object]]:
        rows = self._history_store.list_recent_rows("notification_events", limit=limit)
        latest_by_key: dict[str, dict[str, object]] = {}
        for row in rows:
            dedupe_key = row.get("dedupe_key")
            if not isinstance(dedupe_key, str) or dedupe_key in latest_by_key:
                continue
            latest_by_key[dedupe_key] = row
        pending: list[dict[str, object]] = []
        for row in latest_by_key.values():
            if row.get("status") != "failed" or row.get("needs_retry") is not True:
                continue
            severity = row.get("severity")
            if severity not in ("INFO", "WARN", "CRITICAL"):
                continue
            if not isinstance(row.get("text"), str):
                continue
            pending.append(row)
        pending.sort(key=lambda row: _RETRY_PRIORITY[row["severity"]])
        return pending

    def _collect_sent_notification_keys(self, *, limit: int) -> set[str]:
        rows = self._history_store.list_recent_rows("notification_events", limit=limit)
        sent: set[str] = set()
        for row in rows:
            if row.get("status") != "sent":
                continue
            dedupe_key = row.get("dedupe_key")
            if isinstance(dedupe_key, str):
                sent.add(dedupe_key)
        return sent

    def _collect_proactive_candidates(self, *, limit: int) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        candidates.extend(self._collect_checkpoint_candidates(limit=limit))
        candidates.extend(self._collect_governor_candidates(limit=limit))
        candidates.extend(self._collect_recovery_failure_candidates(limit=limit))
        candidates.extend(self._collect_risk_event_candidates(limit=limit))
        candidates.sort(
            key=lambda item: (
                _PROACTIVE_CATEGORY_PRIORITY.get(item["category"], 99),
                _RETRY_PRIORITY[item["severity"]],
            )
        )
        return candidates

    def _collect_checkpoint_candidates(self, *, limit: int) -> list[dict[str, str]]:
        rows = self._history_store.list_recent_rows("execution_checkpoints", limit=limit)
        candidates: list[dict[str, str]] = []
        for row in rows:
            checkpoint_id = row.get("checkpoint_id")
            current_mode = row.get("current_mode")
            if not isinstance(checkpoint_id, str) or not isinstance(current_mode, str):
                continue
            if current_mode not in RunMode._value2member_map_:
                continue
            mode = RunMode(current_mode)
            if mode == RunMode.NORMAL:
                continue
            severity: NotificationSeverity = "CRITICAL" if mode in (RunMode.REDUCE_ONLY, RunMode.HALTED) else "WARN"
            candidates.append(
                {
                    "category": "mode_change",
                    "dedupe_key": f"checkpoint:{checkpoint_id}:mode:{mode.value}",
                    "severity": severity,
                    "text": format_mode_change(mode),
                }
            )
        return candidates

    def _collect_governor_candidates(self, *, limit: int) -> list[dict[str, str]]:
        snapshot_rows = self._history_store.list_recent_rows("strategy_snapshots", limit=limit)
        snapshots_by_version = {
            row["version_id"]: row for row in snapshot_rows if isinstance(row.get("version_id"), str)
        }
        governor_rows = self._history_store.list_recent_rows("governor_runs", limit=limit)
        candidates: list[dict[str, str]] = []
        for row in governor_rows:
            version_id = row.get("version_id")
            if not isinstance(version_id, str) or row.get("status") != "published":
                continue
            snapshot = snapshots_by_version.get(version_id, {})
            market_mode = snapshot.get("market_mode", "unknown")
            approval = snapshot.get("approval_state", "unknown")
            candidates.append(
                {
                    "category": "snapshot_published",
                    "dedupe_key": f"governor_run:{version_id}:published",
                    "severity": "INFO",
                    "text": f"Snapshot published: {version_id} (mode={market_mode}, approval={approval})",
                }
            )
        return candidates

    def _collect_recovery_failure_candidates(self, *, limit: int) -> list[dict[str, str]]:
        rows = self._history_store.list_recent_rows("risk_events", limit=limit)
        candidates: list[dict[str, str]] = []
        for row in rows:
            event_type = row.get("event_type")
            detail = row.get("detail", "")
            if not isinstance(event_type, str) or "recovery_failed" not in event_type:
                continue
            if not isinstance(detail, str):
                detail = str(detail)
            candidates.append(
                {
                    "category": "recovery_failed",
                    "dedupe_key": f"recovery_failed:{event_type}:{detail}",
                    "severity": "CRITICAL",
                    "text": f"Recovery failed: {detail or event_type}",
                }
            )
        return candidates

    def _collect_risk_event_candidates(self, *, limit: int) -> list[dict[str, str]]:
        rows = self._history_store.list_recent_rows("risk_events", limit=limit)
        candidates: list[dict[str, str]] = []
        for row in rows:
            event_type = row.get("event_type")
            detail = row.get("detail", "")
            if not isinstance(event_type, str):
                continue
            if "recovery_failed" in event_type:
                continue
            if not isinstance(detail, str):
                detail = str(detail)
            candidates.append(
                {
                    "category": "risk_event",
                    "dedupe_key": f"risk_event:{event_type}:{detail}",
                    "severity": "CRITICAL" if "mode_changed" in event_type else "WARN",
                    "text": f"Risk event: {event_type} {detail}".rstrip(),
                }
            )
        return candidates

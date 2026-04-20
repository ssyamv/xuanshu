from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, Protocol

from xuanshu.core.enums import RunMode
from xuanshu.infra.notifier.telegram import TextMessagePayload, render_text_message


_MODE_LABELS: dict[RunMode, str] = {
    RunMode.NORMAL: "正常运行",
    RunMode.DEGRADED: "降级运行",
    RunMode.REDUCE_ONLY: "只减仓",
    RunMode.HALTED: "停止运行",
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
    "trade_order_submitted": 0,
    "trade_order_canceled": 1,
    "trade_position_opened": 2,
    "trade_position_closed": 3,
    "mode_change": 4,
    "governor_research_ready": 5,
    "governor_approval_completed": 6,
    "governor_snapshot_published": 7,
    "recovery_failed": 8,
    "risk_event": 9,
}


def format_mode_change(mode: RunMode) -> str:
    return f"运行模式已切换为{_MODE_LABELS[mode]}"


_SIDE_LABELS = {
    "buy": "买入",
    "sell": "卖出",
}
_INTENT_LABELS = {
    "open": "开仓",
    "close": "平仓",
}


def _format_side(side: object) -> str:
    return _SIDE_LABELS.get(str(side).lower(), str(side))


def _format_intent(intent: object) -> str:
    return _INTENT_LABELS.get(str(intent).lower(), str(intent))


def _format_strategy_line(strategy_id: object) -> str:
    return f"策略：{strategy_id or '未提供'}"


def _format_logic_line(strategy_logic: object) -> str:
    return f"逻辑：{strategy_logic or '未提供'}"


def _infer_strategy_id_from_client_order_id(client_order_id: object) -> str | None:
    normalized = str(client_order_id or "").strip().lower()
    if "breakout" in normalized:
        return "breakout"
    if "meanreversion" in normalized or "mean_reversion" in normalized:
        return "mean_reversion"
    if "riskpause" in normalized or "risk_pause" in normalized:
        return "risk_pause"
    return None


def _default_strategy_logic(strategy_id: object) -> str | None:
    if strategy_id == "breakout":
        return "趋势突破，最近成交偏买方，准备顺势开多。"
    if strategy_id == "mean_reversion":
        return "均值回归，价格偏离后尝试反向回补。"
    if strategy_id == "risk_pause":
        return "风险暂停信号，当前不执行新开仓。"
    return None


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

    def set_manual_release_target(self, mode: str) -> None:
        ...

    def get_manual_release_target(self) -> str | None:
        ...

    def clear_manual_release_target(self) -> None:
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

    def has_notification_event(self, *, dedupe_key: str, status: str | None = None) -> bool:
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
        if command == "/release":
            return render_text_message(self._handle_release_command(text))
        return render_text_message(
            "支持的命令：/status /positions /orders /risk /mode /market /takeover /release"
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
            if self._was_notification_sent(
                dedupe_key=candidate["dedupe_key"],
                recent_sent_keys=sent_notification_keys,
            ):
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
            return "用法：/takeover <degraded|reduce_only|halted> [reason]"
        requested_mode = parts[1].lower()
        if requested_mode not in {"degraded", "reduce_only", "halted"}:
            return "用法：/takeover <degraded|reduce_only|halted> [reason]"
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
        return f"已请求人工接管：{effective_mode.value}（原因：{reason}）"

    def _handle_release_command(self, text: str) -> str:
        parts = text.strip().split(maxsplit=2)
        if len(parts) < 2:
            return "用法：/release <degraded> [reason]"
        requested_mode = parts[1].lower()
        if requested_mode != "degraded":
            return "用法：/release <degraded> [reason]"
        reason = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else "operator approved release"
        self._runtime_store.set_manual_release_target(requested_mode)
        self._history_store.append_risk_event(
            {
                "event_type": "manual_release_requested",
                "symbol": "system",
                "detail": f"requested {requested_mode}: {reason}",
            }
        )
        return f"已请求人工解除：{requested_mode}（原因：{reason}）"

    def _render_status(self) -> str:
        mode = self._render_mode()
        snapshot = self._snapshot_store.get_latest_snapshot()
        snapshot_version = getattr(snapshot, "version_id", "none")
        faults = self._runtime_store.get_fault_flags() or {}
        fault_labels = ", ".join(sorted(faults)) if faults else "none"
        lines = [mode, f"快照版本：{snapshot_version}", f"故障标记：{fault_labels}"]
        budget = self._runtime_store.get_budget_pool_summary()
        if isinstance(budget, dict):
            lines.append(
                "预算："
                f"remaining_notional={budget.get('remaining_notional', 'n/a')} "
                f"remaining_order_count={budget.get('remaining_order_count', 'n/a')}"
            )
        governor_health = self._runtime_store.get_governor_health_summary()
        if isinstance(governor_health, dict):
            lines.append(
                "治理状态："
                f"status={governor_health.get('status', 'unknown')} "
                f"trigger={governor_health.get('trigger', 'unknown')} "
                f"health={governor_health.get('health_state', 'unknown')}"
            )
        return "\n".join(lines)

    def _render_mode(self) -> str:
        mode = self._runtime_store.get_run_mode()
        return f"模式：{_MODE_LABELS[mode] if mode is not None else '未知'}"

    def _render_market(self) -> str:
        lines = list(self._render_symbol_summaries())
        if not lines:
            return "行情：暂无运行摘要"
        return "\n".join(lines)

    def _render_positions(self) -> str:
        rows = self._history_store.list_recent_rows("positions", limit=5)
        if not rows:
            return "仓位：暂无最近仓位记录"
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
            return "订单：暂无最近订单记录"
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
            lines.append("风控：暂无最近风控事件")
        budget = self._runtime_store.get_budget_pool_summary()
        if isinstance(budget, dict):
            lines.append(
                "预算："
                f"remaining_notional={budget.get('remaining_notional', 'n/a')} "
                f"remaining_order_count={budget.get('remaining_order_count', 'n/a')} "
                f"current_mode={budget.get('current_mode', 'n/a')}"
            )
        governor_health = self._runtime_store.get_governor_health_summary()
        if isinstance(governor_health, dict):
            lines.append(
                "治理状态："
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

    def _was_notification_sent(self, *, dedupe_key: str, recent_sent_keys: set[str]) -> bool:
        if dedupe_key in recent_sent_keys:
            return True
        has_notification_event = getattr(self._history_store, "has_notification_event", None)
        if callable(has_notification_event):
            return bool(has_notification_event(dedupe_key=dedupe_key, status="sent"))
        return False

    def _collect_proactive_candidates(self, *, limit: int) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        candidates.extend(self._collect_order_notification_candidates(limit=limit))
        candidates.extend(self._collect_position_notification_candidates(limit=limit))
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

    def _collect_order_notification_candidates(self, *, limit: int) -> list[dict[str, str]]:
        rows = self._history_store.list_recent_rows("orders", limit=limit)
        candidates: list[dict[str, str]] = []
        for row in reversed(rows):
            status = str(row.get("status", "")).strip().lower()
            if status not in {"submitted", "canceled", "cancelled"}:
                continue
            strategy_id = row.get("strategy_id") or _infer_strategy_id_from_client_order_id(row.get("client_order_id"))
            strategy_logic = row.get("strategy_logic") or _default_strategy_logic(strategy_id)
            intent = row.get("intent")
            if not intent and strategy_id is not None:
                intent = "open"
            symbol = str(row.get("symbol", "unknown"))
            side = _format_side(row.get("side", "n/a"))
            intent_label = _format_intent(intent or "unknown")
            client_order_id = str(row.get("client_order_id", "n/a"))
            order_id = str(row.get("order_id", "n/a"))
            text = (
                f"{'订单已提交' if status == 'submitted' else '订单已撤销'}：{symbol} {side}{intent_label}\n"
                f"{_format_strategy_line(strategy_id)}\n"
                f"{_format_logic_line(strategy_logic)}\n"
                f"客户端单号：{client_order_id}\n"
                f"订单号：{order_id}"
            )
            candidates.append(
                {
                    "category": "trade_order_submitted" if status == "submitted" else "trade_order_canceled",
                    "dedupe_key": f"order:{status}:{client_order_id}:{order_id}",
                    "severity": "INFO",
                    "text": text,
                }
            )
        return candidates

    def _collect_position_notification_candidates(self, *, limit: int) -> list[dict[str, str]]:
        rows = self._history_store.list_recent_rows("positions", limit=limit)
        order_context_by_symbol = self._build_recent_order_context_by_symbol(limit=limit * 5)
        candidates: list[dict[str, str]] = []
        previous_by_symbol: dict[str, float] = {}
        for row in reversed(rows):
            symbol = str(row.get("symbol", "unknown"))
            net_quantity = float(row.get("net_quantity", 0.0))
            previous_quantity = previous_by_symbol.get(symbol, 0.0)
            explicit_intent = str(row.get("intent", "")).strip().lower()
            inferred_intent = ""
            if explicit_intent in {"open", "close"}:
                inferred_intent = explicit_intent
            elif previous_quantity == 0.0 and net_quantity != 0.0:
                inferred_intent = "open"
            elif previous_quantity != 0.0 and net_quantity == 0.0:
                inferred_intent = "close"
            previous_by_symbol[symbol] = net_quantity
            strategy_id = row.get("strategy_id")
            strategy_logic = row.get("strategy_logic")
            if not strategy_id or not strategy_logic:
                recent_context = order_context_by_symbol.get(symbol, {})
                strategy_id = strategy_id or recent_context.get("strategy_id")
                strategy_logic = strategy_logic or recent_context.get("strategy_logic")
            if inferred_intent == "open":
                text = (
                    f"已开仓：{symbol} 当前仓位={net_quantity} 均价={row.get('average_price', 'n/a')}\n"
                    f"{_format_strategy_line(strategy_id)}\n"
                    f"{_format_logic_line(strategy_logic)}"
                )
                candidates.append(
                    {
                        "category": "trade_position_opened",
                        "dedupe_key": f"position:open:{symbol}:{row.get('average_price', 'n/a')}:{net_quantity}",
                        "severity": "INFO",
                        "text": text,
                    }
                )
            elif inferred_intent == "close":
                text = (
                    f"已平仓：{symbol} 当前仓位={net_quantity} 浮盈亏={row.get('unrealized_pnl', 'n/a')}\n"
                    f"{_format_strategy_line(strategy_id)}\n"
                    f"{_format_logic_line(strategy_logic)}"
                )
                candidates.append(
                    {
                        "category": "trade_position_closed",
                        "dedupe_key": f"position:close:{symbol}:{row.get('unrealized_pnl', 'n/a')}:{net_quantity}",
                        "severity": "INFO",
                        "text": text,
                    }
                )
        return candidates

    def _build_recent_order_context_by_symbol(self, *, limit: int) -> dict[str, dict[str, str]]:
        rows = self._history_store.list_recent_rows("orders", limit=limit)
        contexts: dict[str, dict[str, str]] = {}
        for row in rows:
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or symbol in contexts:
                continue
            strategy_id = row.get("strategy_id") or _infer_strategy_id_from_client_order_id(row.get("client_order_id"))
            strategy_logic = row.get("strategy_logic") or _default_strategy_logic(strategy_id)
            if strategy_id is None and strategy_logic is None:
                continue
            contexts[symbol] = {
                "strategy_id": str(strategy_id or ""),
                "strategy_logic": str(strategy_logic or ""),
            }
        return contexts

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
            if not isinstance(version_id, str):
                continue
            research_status = str(row.get("research_status", "")).strip()
            research_provider = str(row.get("research_provider", "")).strip() or "unknown"
            validation_status = str(row.get("validation_status", "")).strip() or "unknown"
            approval_status = str(row.get("approval_status", "")).strip() or "unknown"
            approval_decision = str(row.get("approval_decision", "")).strip() or approval_status
            research_candidate_count = row.get("research_candidate_count", 0)
            approved_package_ids = row.get("approved_research_candidate_ids") or []

            if (
                isinstance(research_candidate_count, int)
                and research_candidate_count > 0
                and research_status
                and research_status != "not_requested"
            ):
                package_summary = ",".join(str(item) for item in approved_package_ids) or "none"
                candidates.append(
                    {
                        "category": "governor_research_ready",
                        "dedupe_key": f"governor_run:{version_id}:research_ready",
                        "severity": "INFO",
                        "text": (
                            "治理研究已产出候选："
                            f"{research_candidate_count} 个"
                            f"（provider={research_provider}，status={research_status}，packages={package_summary}）"
                        ),
                    }
                )

            if approval_status in {"approved", "approved_with_guardrails", "rejected", "needs_revision"}:
                candidates.append(
                    {
                        "category": "governor_approval_completed",
                        "dedupe_key": f"governor_run:{version_id}:approval_completed",
                        "severity": "INFO",
                        "text": f"治理自动审批完成：{approval_decision}（validation={validation_status}）",
                    }
                )

            if row.get("status") == "published":
                snapshot = snapshots_by_version.get(version_id, {})
                market_mode = snapshot.get("market_mode", "unknown")
                approval = snapshot.get("approval_state", "unknown")
                candidates.append(
                    {
                        "category": "governor_snapshot_published",
                        "dedupe_key": f"governor_run:{version_id}:published",
                        "severity": "INFO",
                        "text": f"治理快照已发布：{version_id}（模式={market_mode}，审批={approval}）",
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
                    "text": f"恢复流程失败：{detail or event_type}",
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
            if event_type in {"account_snapshot_updated", "signal_blocked"}:
                continue
            if not isinstance(detail, str):
                detail = str(detail)
            candidates.append(
                {
                    "category": "risk_event",
                    "dedupe_key": f"risk_event:{event_type}:{detail}",
                    "severity": "CRITICAL" if "mode_changed" in event_type else "WARN",
                    "text": f"风控事件：{event_type} {detail}".rstrip(),
                }
            )
        return candidates

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
    "recovery_failed": 5,
    "risk_event": 6,
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
    if "short_momentum" in normalized or "shortmomentum" in normalized:
        return "short_momentum"
    if "vol_breakout" in normalized or "volbreakout" in normalized:
        return "vol_breakout"
    if "breakout" in normalized:
        return "breakout"
    if "meanreversion" in normalized or "mean_reversion" in normalized:
        return "mean_reversion"
    if "riskpause" in normalized or "risk_pause" in normalized:
        return "risk_pause"
    return None


def _default_strategy_logic(strategy_id: object) -> str | None:
    if strategy_id == "vol_breakout":
        return "波动率突破，价格突破 ATR 阈值后顺势开多。"
    if strategy_id == "short_momentum":
        return "空头动量破位，价格跌破回看阈值后顺势开空。"
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

    def set_budget_pool_summary(self, summary: dict[str, object]) -> None:
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
        if command == "/help":
            return render_text_message(self._render_help())
        if command == "/status":
            return render_text_message(self._render_status())
        if command == "/mode":
            return render_text_message(self._render_mode())
        if command == "/market":
            return render_text_message(self._render_market())
        if command in {"/positions", "/position"}:
            return render_text_message(self._render_positions())
        if command == "/orders":
            return render_text_message(self._render_orders())
        if command == "/risk":
            return render_text_message(self._render_risk())
        if command == "/takeover":
            return render_text_message(self._handle_takeover_command(text))
        if command == "/release":
            return render_text_message(self._handle_release_command(text))
        if command == "/pause":
            return render_text_message(self._handle_pause_command(text))
        if command in {"/start", "/resume"}:
            return render_text_message(self._handle_start_command(text))
        if command in {"/capital", "/amount"}:
            return render_text_message(self._handle_capital_command(text))
        return render_text_message(self._render_help())

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

    def _render_help(self) -> str:
        return "\n".join(
            [
                "支持的命令：",
                "/status - 查看服务状态、策略、账户权益和持仓摘要",
                "/positions - 查看当前运行态持仓",
                "/orders - 查看最近订单",
                "/risk - 查看最近风控事件",
                "/market - 查看行情摘要",
                "/pause [reason] - 暂停交易并切换为 halted",
                "/start [reason] - 请求恢复交易到 normal",
                "/capital <amount> [reason] - 调整当前策略总金额",
            ]
        )

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

    def _handle_pause_command(self, text: str) -> str:
        parts = text.strip().split(maxsplit=1)
        reason = parts[1].strip() if len(parts) >= 2 and parts[1].strip() else "operator requested pause"
        fault_flags = dict(self._runtime_store.get_fault_flags() or {})
        fault_flags["manual_pause"] = {
            "requested_mode": RunMode.HALTED.value,
            "reason": reason,
        }
        self._runtime_store.set_run_mode(RunMode.HALTED)
        self._runtime_store.set_fault_flags(fault_flags)
        self._history_store.append_risk_event(
            {
                "event_type": "manual_pause_requested",
                "symbol": "system",
                "detail": f"requested halted: {reason}",
            }
        )
        return f"已暂停交易：halted（原因：{reason}）"

    def _handle_start_command(self, text: str) -> str:
        parts = text.strip().split(maxsplit=1)
        reason = parts[1].strip() if len(parts) >= 2 and parts[1].strip() else "operator requested start"
        fault_flags = dict(self._runtime_store.get_fault_flags() or {})
        fault_flags.pop("manual_takeover", None)
        fault_flags.pop("manual_pause", None)
        self._runtime_store.set_fault_flags(fault_flags)
        self._runtime_store.set_run_mode(RunMode.NORMAL)
        self._runtime_store.set_manual_release_target(RunMode.NORMAL.value)
        self._history_store.append_risk_event(
            {
                "event_type": "manual_start_requested",
                "symbol": "system",
                "detail": f"requested normal: {reason}",
            }
        )
        return f"已请求启动交易：normal（原因：{reason}）"

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

    def _handle_capital_command(self, text: str) -> str:
        parts = text.strip().split(maxsplit=2)
        if len(parts) < 2:
            return "用法：/capital <amount> [reason]"
        amount = self._parse_positive_float(parts[1])
        if amount is None:
            return "用法：/capital <amount> [reason]"
        reason = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else "operator adjusted strategy capital"
        summary = dict(self._runtime_store.get_budget_pool_summary() or {})
        summary["strategy_total_amount"] = amount
        summary["manual_strategy_total_amount_override"] = True
        self._runtime_store.set_budget_pool_summary(summary)
        self._history_store.append_risk_event(
            {
                "event_type": "manual_strategy_capital_adjusted",
                "symbol": "system",
                "detail": f"strategy_total_amount={amount}: {reason}",
            }
        )
        return f"已调整当前策略总金额：{amount}（原因：{reason}）"

    @staticmethod
    def _parse_positive_float(raw: str) -> float | None:
        try:
            value = float(raw)
        except ValueError:
            return None
        if value <= 0:
            return None
        return value

    def _render_status(self) -> str:
        mode = self._render_mode()
        snapshot = self._snapshot_store.get_latest_snapshot()
        checkpoint = self._latest_checkpoint()
        snapshot_version = getattr(snapshot, "version_id", None) or checkpoint.get("active_snapshot_version", "none")
        faults = self._runtime_store.get_fault_flags() or {}
        fault_labels = ", ".join(sorted(faults)) if faults else "none"
        lines = [mode, f"快照版本：{snapshot_version}", f"故障标记：{fault_labels}"]
        budget = self._runtime_store.get_budget_pool_summary()
        if isinstance(budget, dict):
            lines.append(f"账户权益：{budget.get('equity', 'n/a')}")
            lines.append(f"策略总金额：{budget.get('strategy_total_amount', budget.get('starting_nav', 'n/a'))}")
            lines.append(f"运行控制：{budget.get('current_mode', 'n/a')}")
        strategy_summary_lines = self._render_strategy_summary(snapshot)
        if not strategy_summary_lines:
            strategy_summary_lines = self._render_runtime_strategy_summary()
        if strategy_summary_lines:
            lines.extend(strategy_summary_lines)
        position_lines = list(self._render_symbol_summaries())
        if position_lines:
            lines.append("运行摘要：")
            lines.extend(position_lines)
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
        runtime_lines = []
        for symbol in self._okx_symbols:
            summary = self._runtime_store.get_symbol_runtime_summary(symbol)
            if summary is None:
                continue
            runtime_lines.append(
                f"{symbol}: 当前净持仓={summary.get('net_quantity', 'n/a')} "
                f"中间价={summary.get('mid_price', 'n/a')} "
                f"运行模式={summary.get('run_mode', 'n/a')} "
                f"挂单数={summary.get('open_order_count', 'n/a')}"
            )
        if runtime_lines:
            return "\n".join(runtime_lines)
        rows = self._history_store.list_recent_rows("positions", limit=5)
        if not rows:
            return "持仓：暂无当前运行态或最近仓位记录"
        return "\n".join(
            f"{row.get('symbol', 'unknown')}: "
            f"净持仓={row.get('net_quantity', row.get('quantity', 'n/a'))} "
            f"均价={row.get('average_price', 'n/a')} "
            f"未实现盈亏={row.get('unrealized_pnl', 'n/a')}"
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

    def _render_strategy_summary(self, snapshot: object) -> list[str]:
        summary = self._extract_snapshot_strategy_summary(snapshot)
        if summary is None:
            return []
        return [
            f"当前策略：{summary['strategies']}",
            (
                "参数："
                f"risk_multiplier={summary['risk_multiplier']} "
                f"per_symbol_max_position={summary['per_symbol_max_position']} "
                f"max_leverage={summary['max_leverage']}"
            ),
            f"标的：{summary['symbols']}",
        ]

    def _latest_checkpoint(self) -> dict[str, object]:
        rows = self._history_store.list_recent_rows("execution_checkpoints", limit=1)
        row = rows[0] if rows else {}
        return row if isinstance(row, dict) else {}

    def _render_runtime_strategy_summary(self) -> list[str]:
        rows = self._history_store.list_recent_rows("orders", limit=20)
        strategy_by_symbol: dict[str, dict[str, str]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").strip()
            if not symbol or symbol in strategy_by_symbol:
                continue
            strategy_id = row.get("strategy_id") or _infer_strategy_id_from_client_order_id(row.get("client_order_id"))
            if strategy_id is None:
                continue
            strategy_logic = row.get("strategy_logic") or _default_strategy_logic(strategy_id) or "未提供"
            strategy_by_symbol[symbol] = {
                "strategy_id": str(strategy_id),
                "strategy_logic": str(strategy_logic),
            }
        if not strategy_by_symbol:
            return [f"标的：{','.join(self._okx_symbols)}"]
        strategies = ", ".join(sorted({item["strategy_id"] for item in strategy_by_symbol.values()}))
        lines = [
            f"当前策略：{strategies}",
            f"标的：{','.join(self._okx_symbols)}",
            "策略逻辑：",
        ]
        for symbol in self._okx_symbols:
            strategy = strategy_by_symbol.get(symbol)
            if strategy is None:
                continue
            lines.append(f"{symbol}: {strategy['strategy_logic']}")
        return lines

    def _extract_snapshot_strategy_summary(self, snapshot: object) -> dict[str, str] | None:
        if snapshot is None:
            return None
        if isinstance(snapshot, dict):
            strategy_enable_flags = snapshot.get("strategy_enable_flags")
            symbol_whitelist = snapshot.get("symbol_whitelist")
            risk_multiplier = snapshot.get("risk_multiplier")
            per_symbol_max_position = snapshot.get("per_symbol_max_position")
            max_leverage = snapshot.get("max_leverage")
            market_mode = snapshot.get("market_mode")
        else:
            strategy_enable_flags = getattr(snapshot, "strategy_enable_flags", None)
            symbol_whitelist = getattr(snapshot, "symbol_whitelist", None)
            risk_multiplier = getattr(snapshot, "risk_multiplier", None)
            per_symbol_max_position = getattr(snapshot, "per_symbol_max_position", None)
            max_leverage = getattr(snapshot, "max_leverage", None)
            market_mode = getattr(snapshot, "market_mode", None)

        if not isinstance(strategy_enable_flags, dict):
            return None
        enabled_strategies = [
            strategy_id
            for strategy_id, enabled in strategy_enable_flags.items()
            if enabled is True and isinstance(strategy_id, str)
        ]
        if not enabled_strategies:
            enabled_strategies = ["none"]

        symbols = ",".join(
            symbol for symbol in symbol_whitelist if isinstance(symbol, str) and symbol.strip()
        ) if isinstance(symbol_whitelist, list) else ""
        if not symbols:
            symbols = "unknown"

        market_mode_value = market_mode.value if isinstance(market_mode, RunMode) else str(market_mode or "unknown")
        return {
            "strategies": ", ".join(enabled_strategies),
            "symbols": symbols,
            "risk_multiplier": str(risk_multiplier if risk_multiplier is not None else "n/a"),
            "per_symbol_max_position": str(per_symbol_max_position if per_symbol_max_position is not None else "n/a"),
            "max_leverage": str(max_leverage if max_leverage is not None else "n/a"),
            "market_mode": market_mode_value,
        }

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
            if event_type.startswith("manual_"):
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

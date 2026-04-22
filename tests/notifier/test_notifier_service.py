import pytest
from pydantic import SecretStr

from xuanshu.core.enums import RunMode
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.infra.notifier.telegram import TelegramBotCommand, TelegramNotifier, TextMessagePayload, render_text_message
from xuanshu.notifier.service import NotifierService, format_mode_change


def test_mode_change_notification_is_human_readable() -> None:
    assert format_mode_change(RunMode.REDUCE_ONLY) == "运行模式已切换为只减仓"


def test_telegram_text_payload_is_typed() -> None:
    payload = render_text_message("hello")

    assert payload == TextMessagePayload(text="hello")
    assert payload.parse_mode is None


class _RuntimeStore:
    def __init__(self) -> None:
        self.mode = RunMode.DEGRADED
        self.symbols = {
            "BTC-USDT-SWAP": {"symbol": "BTC-USDT-SWAP", "mid_price": 100.1, "net_quantity": 1.25},
            "ETH-USDT-SWAP": {"symbol": "ETH-USDT-SWAP", "mid_price": 200.2, "net_quantity": 0.0},
        }
        self.faults = {"public_ws_disconnected": {"severity": "warn"}}
        self.budget = {
            "remaining_notional": 120.0,
            "remaining_order_count": 8,
            "current_mode": "degraded",
            "equity": 918.27,
            "strategy_total_amount": 5000.0,
        }
        self.manual_release_target: str | None = None

    def get_run_mode(self) -> RunMode | None:
        return self.mode

    def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
        return self.symbols.get(symbol)

    def get_fault_flags(self) -> dict[str, object] | None:
        return self.faults

    def get_budget_pool_summary(self) -> dict[str, object] | None:
        return self.budget

    def set_budget_pool_summary(self, summary: dict[str, object]) -> None:
        self.budget = summary

    def set_run_mode(self, mode: RunMode) -> None:
        self.mode = mode

    def set_fault_flags(self, flags: dict[str, object]) -> None:
        self.faults = flags

    def set_manual_release_target(self, mode: str) -> None:
        self.manual_release_target = mode

    def get_manual_release_target(self) -> str | None:
        return self.manual_release_target

    def clear_manual_release_target(self) -> None:
        self.manual_release_target = None


class _SnapshotStore:
    def get_latest_snapshot(self):
        class _Snapshot:
            version_id = "snap-live"
            market_mode = RunMode.DEGRADED
            symbol_whitelist = ["BTC-USDT-SWAP"]
            strategy_enable_flags = {"vol_breakout": True, "risk_pause": True}
            risk_multiplier = 0.25
            per_symbol_max_position = 0.12
            max_leverage = 1

        return _Snapshot()


@pytest.mark.asyncio
async def test_notifier_service_renders_status_and_market_queries_from_runtime_state() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.append_position_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "net_quantity": 1.25,
            "average_price": 99.8,
            "unrealized_pnl": 1.2,
        }
    )
    history.append_order_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "side": "buy",
            "status": "live",
            "client_order_id": "btc-breakout-000001",
        }
    )
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    status = await service.handle_command("/status")
    market = await service.handle_command("/market")
    positions = await service.handle_command("/positions")
    orders = await service.handle_command("/orders")
    risk = await service.handle_command("/risk")

    assert "模式：降级运行" in status.text
    assert "快照版本：snap-live" in status.text
    assert "故障标记：public_ws_disconnected" in status.text
    assert "账户权益：918.27" in status.text
    assert "策略总金额：5000.0" in status.text
    assert "运行控制：degraded" in status.text
    assert "remaining_notional" not in status.text
    assert "available_balance" not in status.text
    assert "当前策略：vol_breakout, risk_pause" in status.text
    assert "参数：risk_multiplier=0.25 per_symbol_max_position=0.12 max_leverage=1" in status.text
    assert "BTC-USDT-SWAP" in market.text
    assert "ETH-USDT-SWAP" in market.text
    assert "BTC-USDT-SWAP: 当前净持仓=1.25" in positions.text
    assert "ETH-USDT-SWAP: 当前净持仓=0.0" in positions.text
    assert "BTC-USDT-SWAP buy live cid=btc-breakout-000001" in orders.text
    assert "预算：" not in risk.text


@pytest.mark.asyncio
async def test_notifier_service_returns_chinese_help_for_english_commands() -> None:
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu"),
    )

    payload = await service.handle_command("/help")

    assert "/positions - 查看当前运行态持仓" in payload.text
    assert "/pause [reason] - 暂停交易并切换为 halted" in payload.text
    assert "/start [reason] - 请求恢复交易到 normal" in payload.text
    assert "/capital <amount> [reason] - 调整当前策略总金额" in payload.text
    assert "/budget" not in payload.text


def test_notifier_service_exposes_telegram_bot_commands() -> None:
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu"),
    )

    commands = service.telegram_bot_commands()

    assert TelegramBotCommand(command="status", description="查看服务、策略、权益和持仓摘要") in commands
    assert TelegramBotCommand(command="takeover", description="请求人工接管到保守模式") in commands
    assert all(command.command == command.command.lower() for command in commands)
    assert all(not command.command.startswith("/") for command in commands)


@pytest.mark.asyncio
async def test_notifier_service_accepts_manual_takeover_command_and_records_audit_trail() -> None:
    runtime = _RuntimeStore()
    runtime.mode = RunMode.NORMAL
    runtime.faults = {}
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=runtime,
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    payload = await service.handle_command("/takeover reduce_only flatten risk manually")

    assert payload.text == "已请求人工接管：reduce_only（原因：flatten risk manually）"
    assert runtime.mode == RunMode.REDUCE_ONLY
    assert runtime.faults == {
        "manual_takeover": {
            "requested_mode": "reduce_only",
            "reason": "flatten risk manually",
        }
    }
    assert history.list_recent_rows("risk_events", limit=1) == [
        {
            "event_type": "manual_takeover_requested",
            "symbol": "system",
            "detail": "requested reduce_only: flatten risk manually",
        }
    ]


@pytest.mark.asyncio
async def test_notifier_service_rejects_manual_takeover_without_supported_mode() -> None:
    runtime = _RuntimeStore()
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=runtime,
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    payload = await service.handle_command("/takeover normal")

    assert payload.text == "用法：/takeover <degraded|reduce_only|halted> [reason]"
    assert runtime.mode == RunMode.DEGRADED
    assert history.list_recent_rows("risk_events", limit=1) == []


@pytest.mark.asyncio
async def test_notifier_service_accepts_pause_and_start_commands() -> None:
    runtime = _RuntimeStore()
    runtime.mode = RunMode.NORMAL
    runtime.faults = {}
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=runtime,
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    pause = await service.handle_command("/pause maintenance")
    start = await service.handle_command("/start checks passed")

    assert pause.text == "已暂停交易：halted（原因：maintenance）"
    assert start.text == "已请求启动交易：normal（原因：checks passed）"
    assert runtime.mode == RunMode.NORMAL
    assert runtime.faults == {}
    assert runtime.manual_release_target == "normal"
    assert history.list_recent_rows("risk_events", limit=2) == [
        {
            "event_type": "manual_start_requested",
            "symbol": "system",
            "detail": "requested normal: checks passed",
        },
        {
            "event_type": "manual_pause_requested",
            "symbol": "system",
            "detail": "requested halted: maintenance",
        },
    ]


@pytest.mark.asyncio
async def test_notifier_service_adjusts_strategy_total_amount_from_english_command() -> None:
    runtime = _RuntimeStore()
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=runtime,
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    payload = await service.handle_command("/capital 5000 operator cap")

    assert payload.text == "已调整当前策略总金额：5000.0（原因：operator cap）"
    assert runtime.budget["strategy_total_amount"] == 5000.0
    assert runtime.budget["manual_strategy_total_amount_override"] is True
    assert history.list_recent_rows("risk_events", limit=1) == [
        {
            "event_type": "manual_strategy_capital_adjusted",
            "symbol": "system",
            "detail": "strategy_total_amount=5000.0: operator cap",
        }
    ]


@pytest.mark.asyncio
async def test_notifier_status_falls_back_to_checkpoint_and_order_strategy_context() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.save_checkpoint(
        {
            "checkpoint_id": "runtime",
            "active_snapshot_version": "fixed-vol-breakout-eth4h-btc12h-20260421T1011Z",
            "current_mode": "normal",
            "needs_reconcile": False,
        }
    )
    history.append_order_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "side": "buy",
            "status": "submitted",
            "client_order_id": "BTCUSDTSWAPvolbreakout000004",
            "order_id": "ord-btc",
            "intent": "open",
            "strategy_id": "vol_breakout",
            "strategy_logic": "BTC-USDT-SWAP 12H 波动率突破，价格突破 ATR 阈值后顺势开多。",
        }
    )
    history.append_order_fact(
        {
            "symbol": "ETH-USDT-SWAP",
            "side": "buy",
            "status": "submitted",
            "client_order_id": "ETHUSDTSWAPvolbreakout000716",
            "order_id": "ord-eth",
            "intent": "open",
            "strategy_id": "vol_breakout",
            "strategy_logic": "ETH 4H 波动率突破，价格突破 ATR 阈值后顺势开多。",
        }
    )

    class _EmptySnapshotStore:
        def get_latest_snapshot(self):
            return None

    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
        runtime_store=_RuntimeStore(),
        snapshot_store=_EmptySnapshotStore(),
        history_store=history,
    )

    status = await service.handle_command("/status")

    assert "快照版本：fixed-vol-breakout-eth4h-btc12h-20260421T1011Z" in status.text
    assert "账户权益：918.27" in status.text
    assert "策略总金额：5000.0" in status.text
    assert "运行控制：degraded" in status.text
    assert "remaining_notional" not in status.text
    assert "available_balance" not in status.text
    assert "当前策略：vol_breakout" in status.text
    assert "策略逻辑：" in status.text
    assert "BTC-USDT-SWAP: BTC-USDT-SWAP 12H 波动率突破" in status.text
    assert "ETH-USDT-SWAP: ETH 4H 波动率突破" in status.text
    assert "运行摘要：" in status.text


@pytest.mark.asyncio
async def test_notifier_service_accepts_manual_release_to_degraded() -> None:
    runtime = _RuntimeStore()
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=runtime,
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    payload = await service.handle_command("/release degraded operator approved first release")

    assert payload.text == "已请求人工解除：degraded（原因：operator approved first release）"
    assert runtime.manual_release_target == "degraded"
    assert history.list_recent_rows("risk_events", limit=1) == [
        {
            "event_type": "manual_release_requested",
            "symbol": "system",
            "detail": "requested degraded: operator approved first release",
        }
    ]


@pytest.mark.asyncio
async def test_notifier_service_records_failed_critical_delivery_for_retry() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    class _Adapter:
        def __init__(self) -> None:
            self.calls = 0

        async def send_text(self, payload: TextMessagePayload) -> None:
            self.calls += 1
            raise RuntimeError("telegram down")

    with pytest.raises(RuntimeError, match="telegram down"):
        await service.deliver_text(
            adapter=_Adapter(),
            text="进入 halted 模式",
            severity="CRITICAL",
            category="mode_change",
            dedupe_key="mode:halted",
        )

    assert history.written_rows["notification_events"] == [
        {
            "category": "mode_change",
            "dedupe_key": "mode:halted",
            "severity": "CRITICAL",
            "status": "failed",
            "attempt_count": 3,
            "needs_retry": True,
            "text": "进入 halted 模式",
        }
    ]


@pytest.mark.asyncio
async def test_notifier_service_flushes_pending_retry_notifications() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.append_notification_event(
        {
            "category": "mode_change",
            "dedupe_key": "mode:halted",
            "severity": "CRITICAL",
            "status": "failed",
            "attempt_count": 3,
            "needs_retry": True,
            "text": "进入 halted 模式",
        }
    )
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    delivered: list[str] = []

    class _Adapter:
        async def send_text(self, payload: TextMessagePayload) -> None:
            delivered.append(payload.text)

    flushed = await service.flush_pending_notifications(adapter=_Adapter())

    assert flushed == 1
    assert delivered == ["进入 halted 模式"]
    assert history.written_rows["notification_events"][-1] == {
        "category": "mode_change",
        "dedupe_key": "mode:halted",
        "severity": "CRITICAL",
        "status": "sent",
        "attempt_count": 1,
        "needs_retry": False,
        "text": "进入 halted 模式",
    }


@pytest.mark.asyncio
async def test_notifier_service_prioritizes_critical_retries_and_skips_resolved_keys() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.append_notification_event(
        {
            "category": "latency",
            "dedupe_key": "warn:latency",
            "severity": "WARN",
            "status": "failed",
            "attempt_count": 1,
            "needs_retry": True,
            "text": "latency elevated",
        }
    )
    history.append_notification_event(
        {
            "category": "mode_change",
            "dedupe_key": "critical:halted",
            "severity": "CRITICAL",
            "status": "failed",
            "attempt_count": 3,
            "needs_retry": True,
            "text": "进入 halted 模式",
        }
    )
    history.append_notification_event(
        {
            "category": "mode_change",
            "dedupe_key": "warn:latency",
            "severity": "WARN",
            "status": "sent",
            "attempt_count": 1,
            "needs_retry": False,
            "text": "latency elevated",
        }
    )
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    delivered: list[str] = []

    class _Adapter:
        async def send_text(self, payload: TextMessagePayload) -> None:
            delivered.append(payload.text)

    flushed = await service.flush_pending_notifications(adapter=_Adapter())

    assert flushed == 1
    assert delivered == ["进入 halted 模式"]


@pytest.mark.asyncio
async def test_notifier_service_emits_proactive_notifications_from_history_rows() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.save_checkpoint(
        {
            "checkpoint_id": "recovery-001",
            "current_mode": "reduce_only",
            "needs_reconcile": False,
        }
    )
    history.append_risk_event(
        {
            "event_type": "runtime_mode_changed",
            "symbol": "system",
            "detail": "startup gating tightened runtime to reduce_only",
        }
    )
    history.append_risk_event(
        {
            "event_type": "startup_recovery_failed",
            "symbol": "system",
            "detail": "exchange_state_mismatch",
        }
    )
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    delivered: list[str] = []

    class _Adapter:
        async def send_text(self, payload: TextMessagePayload) -> None:
            delivered.append(payload.text)

    flushed = await service.flush_proactive_notifications(adapter=_Adapter())

    assert flushed == 3
    assert delivered == [
        "运行模式已切换为只减仓",
        "恢复流程失败：exchange_state_mismatch",
        "风控事件：runtime_mode_changed startup gating tightened runtime to reduce_only",
    ]


@pytest.mark.asyncio
async def test_notifier_service_emits_chinese_trade_notifications_for_submit_cancel_open_and_close() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.append_order_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "side": "buy",
            "status": "submitted",
            "client_order_id": "BTCUSDTSWAPvolbreakout000001",
            "order_id": "ord-open-1",
            "intent": "open",
            "strategy_id": "vol_breakout",
            "strategy_logic": "BTC-USDT-SWAP 12H 波动率突破，价格突破 ATR 阈值后顺势开多。",
        }
    )
    history.append_order_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "side": "sell",
            "status": "canceled",
            "client_order_id": "BTCUSDTSWAPvolbreakout000002",
            "order_id": "ord-close-1",
            "intent": "close",
            "strategy_id": "vol_breakout",
            "strategy_logic": "平仓挂单超时未成交，已撤单等待下一次机会。",
        }
    )
    history.append_position_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "net_quantity": 0.0,
            "average_price": 0.0,
            "unrealized_pnl": 0.0,
        }
    )
    history.append_position_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "net_quantity": 2.0,
            "average_price": 100.2,
            "unrealized_pnl": 0.3,
            "intent": "open",
            "strategy_id": "vol_breakout",
            "strategy_logic": "BTC-USDT-SWAP 12H 波动率突破已确认，完成开仓。",
        }
    )
    history.append_position_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "net_quantity": 0.0,
            "average_price": 0.0,
            "unrealized_pnl": 1.1,
            "intent": "close",
            "strategy_id": "vol_breakout",
            "strategy_logic": "达到平仓条件，已落袋离场。",
        }
    )
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    delivered: list[str] = []

    class _Adapter:
        async def send_text(self, payload: TextMessagePayload) -> None:
            delivered.append(payload.text)

    flushed = await service.flush_proactive_notifications(adapter=_Adapter(), limit=10)

    assert flushed == 4
    assert delivered == [
        "订单已提交：BTC-USDT-SWAP 买入开仓\n策略：vol_breakout\n逻辑：BTC-USDT-SWAP 12H 波动率突破，价格突破 ATR 阈值后顺势开多。\n客户端单号：BTCUSDTSWAPvolbreakout000001\n订单号：ord-open-1",
        "订单已撤销：BTC-USDT-SWAP 卖出平仓\n策略：vol_breakout\n逻辑：平仓挂单超时未成交，已撤单等待下一次机会。\n客户端单号：BTCUSDTSWAPvolbreakout000002\n订单号：ord-close-1",
        "已开仓：BTC-USDT-SWAP 当前仓位=2.0 均价=100.2\n策略：vol_breakout\n逻辑：BTC-USDT-SWAP 12H 波动率突破已确认，完成开仓。",
        "已平仓：BTC-USDT-SWAP 当前仓位=0.0 浮盈亏=1.1\n策略：vol_breakout\n逻辑：达到平仓条件，已落袋离场。",
    ]


@pytest.mark.asyncio
async def test_notifier_service_backfills_order_strategy_logic_and_intent_from_client_order_id() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.append_order_fact(
        {
            "symbol": "ETH-USDT-SWAP",
            "side": "sell",
            "status": "submitted",
            "client_order_id": "ETHUSDTSWAPmeanreversion000002",
            "order_id": "ord-eth-1",
        }
    )
    service = NotifierService(
        okx_symbols=("ETH-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    delivered: list[str] = []

    class _Adapter:
        async def send_text(self, payload: TextMessagePayload) -> None:
            delivered.append(payload.text)

    flushed = await service.flush_proactive_notifications(adapter=_Adapter(), limit=10)

    assert flushed == 1
    assert delivered == [
        "订单已提交：ETH-USDT-SWAP 卖出开仓\n策略：mean_reversion\n逻辑：均值回归，价格偏离后尝试反向回补。\n客户端单号：ETHUSDTSWAPmeanreversion000002\n订单号：ord-eth-1",
    ]


@pytest.mark.asyncio
async def test_notifier_service_backfills_position_strategy_logic_from_recent_order_context() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.append_order_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "side": "buy",
            "status": "submitted",
            "client_order_id": "BTCUSDTSWAPvolbreakout004534",
            "order_id": "ord-btc-1",
        }
    )
    history.append_position_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "net_quantity": 0.0,
            "average_price": 0.0,
            "unrealized_pnl": 0.0,
        }
    )
    history.append_position_fact(
        {
            "symbol": "BTC-USDT-SWAP",
            "net_quantity": 3.5,
            "average_price": 75215.1,
            "unrealized_pnl": 0.0,
        }
    )
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    delivered: list[str] = []

    class _Adapter:
        async def send_text(self, payload: TextMessagePayload) -> None:
            delivered.append(payload.text)

    flushed = await service.flush_proactive_notifications(adapter=_Adapter(), limit=10)

    assert flushed == 2
    assert delivered[0] == "订单已提交：BTC-USDT-SWAP 买入开仓\n策略：vol_breakout\n逻辑：波动率突破，价格突破 ATR 阈值后顺势开多。\n客户端单号：BTCUSDTSWAPvolbreakout004534\n订单号：ord-btc-1"
    assert delivered[1] == "已开仓：BTC-USDT-SWAP 当前仓位=3.5 均价=75215.1\n策略：vol_breakout\n逻辑：波动率突破，价格突破 ATR 阈值后顺势开多。"


@pytest.mark.asyncio
async def test_notifier_service_ignores_account_snapshot_updated_risk_events_for_proactive_notifications() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.append_risk_event(
        {
            "event_type": "account_snapshot_updated",
            "symbol": "system",
            "detail": "equity=918.27",
        }
    )
    history.append_risk_event(
        {
            "event_type": "runtime_mode_changed",
            "symbol": "system",
            "detail": "startup gating tightened runtime to reduce_only",
        }
    )
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    delivered: list[str] = []

    class _Adapter:
        async def send_text(self, payload: TextMessagePayload) -> None:
            delivered.append(payload.text)

    flushed = await service.flush_proactive_notifications(adapter=_Adapter())

    assert flushed == 1
    assert delivered == [
        "风控事件：runtime_mode_changed startup gating tightened runtime to reduce_only",
    ]


@pytest.mark.asyncio
async def test_notifier_service_ignores_signal_blocked_risk_events_for_proactive_notifications() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.append_risk_event(
        {
            "event_type": "signal_blocked",
            "symbol": "BTC-USDT-SWAP",
            "detail": "strategy_disabled",
        }
    )
    history.append_risk_event(
        {
            "event_type": "runtime_mode_changed",
            "symbol": "system",
            "detail": "startup gating tightened runtime to reduce_only",
        }
    )
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    delivered: list[str] = []

    class _Adapter:
        async def send_text(self, payload: TextMessagePayload) -> None:
            delivered.append(payload.text)

    flushed = await service.flush_proactive_notifications(adapter=_Adapter())

    assert flushed == 1
    assert delivered == [
        "风控事件：runtime_mode_changed startup gating tightened runtime to reduce_only",
    ]


@pytest.mark.asyncio
async def test_notifier_service_ignores_manual_command_audit_risk_events_for_proactive_notifications() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.append_risk_event(
        {
            "event_type": "manual_strategy_capital_adjusted",
            "symbol": "system",
            "detail": "strategy_total_amount=5000.0: verify",
        }
    )
    history.append_risk_event(
        {
            "event_type": "manual_pause_requested",
            "symbol": "system",
            "detail": "requested halted: verify",
        }
    )
    history.append_risk_event(
        {
            "event_type": "runtime_mode_changed",
            "symbol": "system",
            "detail": "startup gating tightened runtime to reduce_only",
        }
    )
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    delivered: list[str] = []

    class _Adapter:
        async def send_text(self, payload: TextMessagePayload) -> None:
            delivered.append(payload.text)

    flushed = await service.flush_proactive_notifications(adapter=_Adapter())

    assert flushed == 1
    assert delivered == [
        "风控事件：runtime_mode_changed startup gating tightened runtime to reduce_only",
    ]


@pytest.mark.asyncio
async def test_notifier_service_marks_recovery_failures_as_critical_retriable_notifications() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.append_risk_event(
        {
            "event_type": "startup_recovery_failed",
            "symbol": "system",
            "detail": "exchange_state_mismatch",
        }
    )
    service = NotifierService(
        okx_symbols=("BTC-USDT-SWAP",),
        runtime_store=_RuntimeStore(),
        snapshot_store=_SnapshotStore(),
        history_store=history,
    )

    class _Adapter:
        async def send_text(self, payload: TextMessagePayload) -> None:
            raise RuntimeError("telegram down")

    with pytest.raises(RuntimeError, match="telegram down"):
        await service.flush_proactive_notifications(adapter=_Adapter())

    assert history.written_rows["notification_events"][-1] == {
        "category": "recovery_failed",
        "dedupe_key": "recovery_failed:startup_recovery_failed:exchange_state_mismatch",
        "severity": "CRITICAL",
        "status": "failed",
        "attempt_count": 3,
        "needs_retry": True,
        "text": "恢复流程失败：exchange_state_mismatch",
    }


@pytest.mark.asyncio
async def test_telegram_notifier_send_text_makes_http_request() -> None:
    calls = []

    class _Client:
        async def post(self, url, json):
            calls.append((url, json))

    notifier = TelegramNotifier(
        bot_token=SecretStr("token"),
        chat_id="123",
        client=_Client(),
    )

    await notifier.send_text(TextMessagePayload(text="hello"))

    assert calls == [(
        "https://api.telegram.org/bottoken/sendMessage",
        {"chat_id": "123", "text": "hello"},
    )]


@pytest.mark.asyncio
async def test_telegram_notifier_set_commands_makes_http_request() -> None:
    calls = []

    class _Client:
        async def post(self, url, json):
            calls.append((url, json))

    notifier = TelegramNotifier(
        bot_token=SecretStr("token"),
        chat_id="123",
        client=_Client(),
    )

    await notifier.set_commands(
        [
            TelegramBotCommand(command="status", description="查看服务状态"),
            TelegramBotCommand(command="pause", description="暂停交易"),
        ]
    )

    assert calls == [
        (
            "https://api.telegram.org/bottoken/setMyCommands",
            {
                "commands": [
                    {"command": "status", "description": "查看服务状态"},
                    {"command": "pause", "description": "暂停交易"},
                ]
            },
        )
    ]

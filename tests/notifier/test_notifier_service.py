import pytest
from pydantic import SecretStr

from xuanshu.core.enums import RunMode
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.infra.notifier.telegram import TelegramNotifier, TextMessagePayload, render_text_message
from xuanshu.notifier.service import NotifierService, format_mode_change


def test_mode_change_notification_is_human_readable() -> None:
    assert format_mode_change(RunMode.REDUCE_ONLY) == "Mode changed to reduce-only"


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
        self.budget = {"remaining_notional": 120.0, "remaining_order_count": 8, "current_mode": "degraded"}
        self.governor_health = {"status": "published", "trigger": "risk_event", "health_state": "healthy"}

    def get_run_mode(self) -> RunMode | None:
        return self.mode

    def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
        return self.symbols.get(symbol)

    def get_fault_flags(self) -> dict[str, object] | None:
        return self.faults

    def get_budget_pool_summary(self) -> dict[str, object] | None:
        return self.budget

    def get_governor_health_summary(self) -> dict[str, object] | None:
        return self.governor_health

    def set_run_mode(self, mode: RunMode) -> None:
        self.mode = mode

    def set_fault_flags(self, flags: dict[str, object]) -> None:
        self.faults = flags


class _SnapshotStore:
    def get_latest_snapshot(self):
        class _Snapshot:
            version_id = "snap-live"

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

    assert "Mode: degraded" in status.text
    assert "Snapshot: snap-live" in status.text
    assert "Faults: public_ws_disconnected" in status.text
    assert "Budget: remaining_notional=120.0 remaining_order_count=8" in status.text
    assert "Governor: status=published trigger=risk_event health=healthy" in status.text
    assert "BTC-USDT-SWAP" in market.text
    assert "ETH-USDT-SWAP" in market.text
    assert "BTC-USDT-SWAP: net=1.25 avg=99.8 upnl=1.2" in positions.text
    assert "BTC-USDT-SWAP buy live cid=btc-breakout-000001" in orders.text
    assert "Budget: remaining_notional=120.0 remaining_order_count=8 current_mode=degraded" in risk.text
    assert "Governor: status=published trigger=risk_event health=healthy" in risk.text


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

    assert payload.text == "Manual takeover requested: reduce_only (reason=flatten risk manually)"
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

    assert payload.text == "Usage: /takeover <degraded|reduce_only|halted> [reason]"
    assert runtime.mode == RunMode.DEGRADED
    assert history.list_recent_rows("risk_events", limit=1) == []


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
            text="entered halted mode",
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
            "text": "entered halted mode",
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
            "text": "entered halted mode",
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
    assert delivered == ["entered halted mode"]
    assert history.written_rows["notification_events"][-1] == {
        "category": "mode_change",
        "dedupe_key": "mode:halted",
        "severity": "CRITICAL",
        "status": "sent",
        "attempt_count": 1,
        "needs_retry": False,
        "text": "entered halted mode",
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
            "text": "entered halted mode",
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
    assert delivered == ["entered halted mode"]


@pytest.mark.asyncio
async def test_notifier_service_emits_proactive_notifications_from_history_rows() -> None:
    history = PostgresRuntimeStore(dsn="postgresql://xuanshu:xuanshu@localhost:5432/xuanshu")
    history.append_strategy_snapshot(
        {
            "version_id": "snap-002",
            "market_mode": "degraded",
            "approval_state": "approved",
        }
    )
    history.append_governor_run(
        {
            "version_id": "snap-002",
            "status": "published",
        }
    )
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
        "Mode changed to reduce-only",
        "Snapshot published: snap-002 (mode=degraded, approval=approved)",
        "Risk event: runtime_mode_changed startup gating tightened runtime to reduce_only",
    ]


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

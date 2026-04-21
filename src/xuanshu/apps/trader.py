from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from xuanshu.checkpoints.service import CheckpointService
from xuanshu.config.settings import TraderRuntimeSettings
from xuanshu.core.enums import ApprovalState, EntryType, OkxAccountMode, OrderSide, RunMode, StrategyId, TraderEventType
from xuanshu.contracts.checkpoint import (
    CheckpointBudgetState,
    CheckpointOrder,
    CheckpointPosition,
    ExecutionCheckpoint,
)
from xuanshu.contracts.events import (
    AccountSnapshotEvent,
    FaultEvent,
    MarketTradeEvent,
    OrderUpdateEvent,
    OrderbookTopEvent,
    PositionUpdateEvent,
)
from xuanshu.contracts.strategy import ApprovedStrategyBinding, StrategyConfigSnapshot
from xuanshu.contracts.risk import CandidateSignal
from xuanshu.execution.coordinator import ExecutionCoordinator
from xuanshu.execution.engine import build_client_order_id
from xuanshu.infra.okx.private_ws import OkxPrivateStream
from xuanshu.infra.okx.public_ws import OkxPublicStream
from xuanshu.infra.okx.rest import OkxRestClient
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.infra.storage.redis_store import (
    RedisRuntimeStateStore,
    RedisSnapshotStore,
    RuntimeStateStore,
    SnapshotStore,
)
from xuanshu.risk.kernel import RiskKernel
from xuanshu.state.engine import StateEngine
from xuanshu.strategies.signals import build_candidate_signals
from xuanshu.ops.runtime_logging import configure_runtime_logger
from xuanshu.trader.dispatcher import build_strategy_handover_event_order, dispatch_event
from xuanshu.trader.recovery import RecoverySupervisor
from xuanshu.risk.kernel import is_stronger_strategy_replacement

_OKX_REST_BASE_URL = "https://www.okx.com"
_OKX_PUBLIC_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
_OKX_PRIVATE_WS_URL = "wss://ws.okx.com:8443/ws/v5/private"
_OKX_DEMO_PUBLIC_WS_URL = "wss://wspap.okx.com:8443/ws/v5/public"
_OKX_DEMO_PRIVATE_WS_URL = "wss://wspap.okx.com:8443/ws/v5/private"
_LOGGER = configure_runtime_logger("xuanshu.trader")
_RUN_MODE_PRIORITY = {
    RunMode.NORMAL: 0,
    RunMode.DEGRADED: 1,
    RunMode.REDUCE_ONLY: 2,
    RunMode.HALTED: 3,
}


@dataclass(frozen=True, slots=True)
class TraderComponents:
    state_engine: StateEngine
    risk_kernel: RiskKernel
    checkpoint_service: CheckpointService
    okx_rest_client: OkxRestClient
    okx_public_stream: OkxPublicStream
    okx_private_stream: OkxPrivateStream
    client_order_id_builder: Callable[[str, str, int], str]

    async def aclose(self) -> None:
        await self.okx_rest_client.aclose()


@dataclass(slots=True)
class TraderRuntime:
    settings: TraderRuntimeSettings
    components: TraderComponents
    snapshot_store: SnapshotStore
    runtime_store: RuntimeStateStore
    history_store: PostgresRuntimeStore
    execution_coordinator: ExecutionCoordinator
    recovery_supervisor: RecoverySupervisor
    starting_nav: float
    startup_snapshot: StrategyConfigSnapshot
    startup_checkpoint: ExecutionCheckpoint
    active_symbol_strategies: dict[str, ApprovedStrategyBinding] = field(default_factory=dict)
    symbol_handover_state: dict[str, dict[str, object]] = field(default_factory=dict)
    current_mode: RunMode = RunMode.NORMAL
    opening_allowed: bool = True
    execution_sequence: int = 0


def _build_startup_snapshot(settings: TraderRuntimeSettings) -> StrategyConfigSnapshot:
    generated_at = datetime.now(UTC)
    return StrategyConfigSnapshot(
        version_id="bootstrap",
        generated_at=generated_at,
        effective_from=generated_at,
        expires_at=generated_at + timedelta(minutes=5),
        symbol_whitelist=list(settings.okx_symbols),
        strategy_enable_flags={"breakout": True, "mean_reversion": False, "risk_pause": True},
        risk_multiplier=0.5,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=settings.default_run_mode,
        approval_state=ApprovalState.APPROVED,
        source_reason="bootstrap",
        ttl_sec=300,
    )


def _build_startup_checkpoint(startup_snapshot: StrategyConfigSnapshot, current_mode: RunMode) -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        checkpoint_id="startup",
        created_at=datetime.now(UTC),
        active_snapshot_version=startup_snapshot.version_id,
        current_mode=current_mode,
        positions_snapshot=[],
        open_orders_snapshot=[],
        budget_state=CheckpointBudgetState(
            max_daily_loss=100.0,
            remaining_daily_loss=100.0,
            remaining_notional=100.0,
            remaining_order_count=10,
        ),
        last_public_stream_marker=None,
        last_private_stream_marker=None,
        needs_reconcile=False,
    )


def build_trader_components(settings: TraderRuntimeSettings) -> TraderComponents:
    simulated_trading = settings.okx_account_mode == OkxAccountMode.DEMO
    public_ws_url = _OKX_DEMO_PUBLIC_WS_URL if simulated_trading else _OKX_PUBLIC_WS_URL
    private_ws_url = _OKX_DEMO_PRIVATE_WS_URL if simulated_trading else _OKX_PRIVATE_WS_URL
    return TraderComponents(
        state_engine=StateEngine(),
        risk_kernel=RiskKernel(nav=settings.trader_starting_nav),
        checkpoint_service=CheckpointService(),
        okx_rest_client=OkxRestClient(
            base_url=_OKX_REST_BASE_URL,
            api_key=settings.okx_api_key.get_secret_value(),
            api_secret=settings.okx_api_secret.get_secret_value(),
            passphrase=settings.okx_api_passphrase.get_secret_value(),
            simulated_trading=simulated_trading,
        ),
        okx_public_stream=OkxPublicStream(url=public_ws_url),
        okx_private_stream=OkxPrivateStream(
            url=private_ws_url,
            simulated_trading=simulated_trading,
        ),
        client_order_id_builder=build_client_order_id,
    )


def build_snapshot_store(settings: TraderRuntimeSettings) -> SnapshotStore:
    return RedisSnapshotStore(redis_url=str(settings.redis_url))


def build_runtime_state_store(settings: TraderRuntimeSettings) -> RuntimeStateStore:
    return RedisRuntimeStateStore(redis_url=str(settings.redis_url))


def build_history_store(settings: TraderRuntimeSettings) -> PostgresRuntimeStore:
    return PostgresRuntimeStore(dsn=str(settings.postgres_dsn))


def _more_restrictive_mode(left: RunMode, right: RunMode) -> RunMode:
    if _RUN_MODE_PRIORITY[left] >= _RUN_MODE_PRIORITY[right]:
        return left
    return right


def build_trader_runtime() -> TraderRuntime:
    settings = TraderRuntimeSettings()
    components = build_trader_components(settings)
    snapshot_store = build_snapshot_store(settings)
    startup_snapshot = _build_startup_snapshot(settings)
    latest_snapshot = snapshot_store.get_latest_snapshot()
    if latest_snapshot is not None:
        startup_snapshot = latest_snapshot.model_copy(
            update={
                "market_mode": _more_restrictive_mode(
                    settings.default_run_mode,
                    latest_snapshot.market_mode,
                )
            }
        )
    initial_mode = startup_snapshot.market_mode
    return TraderRuntime(
        settings=settings,
        components=components,
        snapshot_store=snapshot_store,
        runtime_store=build_runtime_state_store(settings),
        history_store=build_history_store(settings),
        execution_coordinator=ExecutionCoordinator(rest_client=components.okx_rest_client),
        recovery_supervisor=RecoverySupervisor(rest_client=components.okx_rest_client),
        starting_nav=settings.trader_starting_nav,
        startup_snapshot=startup_snapshot,
        startup_checkpoint=_build_startup_checkpoint(startup_snapshot, initial_mode),
        active_symbol_strategies=dict(startup_snapshot.symbol_strategy_bindings),
        current_mode=initial_mode,
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


def _now_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _build_runtime_budget_summary(runtime: TraderRuntime) -> dict[str, object]:
    return {
        "max_daily_loss": runtime.startup_checkpoint.budget_state.max_daily_loss,
        "remaining_daily_loss": runtime.startup_checkpoint.budget_state.remaining_daily_loss,
        "remaining_notional": runtime.startup_checkpoint.budget_state.remaining_notional,
        "remaining_order_count": runtime.startup_checkpoint.budget_state.remaining_order_count,
        "current_mode": runtime.current_mode.value,
        "starting_nav": runtime.starting_nav,
        **runtime.components.state_engine.build_budget_pool_summary(),
    }


def _build_execution_checkpoint(runtime: TraderRuntime, *, checkpoint_id: str) -> ExecutionCheckpoint:
    positions_snapshot = [
        CheckpointPosition(
            symbol=symbol,
            net_quantity=position.net_quantity,
            mark_price=position.mark_price,
            unrealized_pnl=position.unrealized_pnl,
        )
        for symbol, position in sorted(runtime.components.state_engine.positions_by_symbol.items())
    ]
    open_orders_snapshot = [
        CheckpointOrder(
            order_id=order.order_id,
            symbol=symbol,
            side=OrderSide(order.side),
            price=order.price,
            size=order.size,
            status=order.status,
        )
        for symbol, orders in sorted(runtime.components.state_engine.open_orders_by_symbol.items())
        for order in sorted(orders.values(), key=lambda value: value.order_id)
    ]
    return ExecutionCheckpoint(
        checkpoint_id=checkpoint_id,
        created_at=datetime.now(UTC),
        active_snapshot_version=runtime.startup_snapshot.version_id,
        current_mode=runtime.current_mode,
        positions_snapshot=positions_snapshot,
        open_orders_snapshot=open_orders_snapshot,
        budget_state=runtime.startup_checkpoint.budget_state,
        last_public_stream_marker=runtime.components.state_engine.last_public_stream_marker,
        last_private_stream_marker=runtime.components.state_engine.last_private_stream_marker,
        needs_reconcile=runtime.startup_checkpoint.needs_reconcile,
    )


def _load_latest_checkpoint(runtime: TraderRuntime) -> ExecutionCheckpoint:
    rows = runtime.history_store.list_recent_rows("execution_checkpoints", limit=1)
    if not rows:
        return runtime.startup_checkpoint
    latest_row = dict(rows[0])
    latest_row.setdefault("created_at", datetime.now(UTC))
    latest_row.setdefault("active_snapshot_version", runtime.startup_snapshot.version_id)
    latest_row.setdefault("current_mode", runtime.current_mode.value)
    latest_row.setdefault("positions_snapshot", [])
    latest_row.setdefault("open_orders_snapshot", [])
    latest_row.setdefault(
        "budget_state",
        runtime.startup_checkpoint.budget_state.model_dump(mode="json"),
    )
    latest_row.setdefault("needs_reconcile", False)
    try:
        return ExecutionCheckpoint.model_validate(latest_row)
    except Exception:
        return runtime.startup_checkpoint


def _publish_runtime_state(runtime: TraderRuntime, *, symbol: str | None = None) -> None:
    if symbol:
        runtime.runtime_store.set_symbol_runtime_summary(
            symbol,
            runtime.components.state_engine.build_symbol_runtime_summary(symbol),
        )
    runtime.runtime_store.set_run_mode(runtime.components.state_engine.current_run_mode)
    runtime.runtime_store.set_fault_flags(runtime.components.state_engine.fault_flags)
    runtime.runtime_store.set_budget_pool_summary(_build_runtime_budget_summary(runtime))


def _tighten_runtime_mode(runtime: TraderRuntime, target_mode: RunMode) -> None:
    tightened = _more_restrictive_mode(runtime.current_mode, target_mode)
    previous_mode = runtime.current_mode
    runtime.current_mode = tightened
    runtime.components.state_engine.set_run_mode(tightened)
    runtime.startup_checkpoint.current_mode = tightened
    runtime.opening_allowed = tightened not in {RunMode.REDUCE_ONLY, RunMode.HALTED}
    if tightened != previous_mode:
        _LOGGER.info(
            "runtime_mode_changed",
            extra={
                "service": "trader",
                "previous_mode": previous_mode.value,
                "current_mode": tightened.value,
            },
        )


def _fault_mode(event: FaultEvent) -> RunMode:
    if "private" in event.code or event.severity == "critical":
        return RunMode.REDUCE_ONLY
    if "public" in event.code or event.severity == "warn":
        return RunMode.DEGRADED
    return RunMode.NORMAL


def _next_client_order_id(runtime: TraderRuntime, signal: CandidateSignal) -> str:
    runtime.execution_sequence += 1
    return runtime.components.client_order_id_builder(
        signal.symbol,
        signal.strategy_id.value,
        runtime.execution_sequence,
    )


def _build_strategy_logic(signal: CandidateSignal) -> str:
    if signal.strategy_id == StrategyId.BREAKOUT:
        return "趋势突破，最近成交偏买方，准备顺势开多。"
    if signal.strategy_id == StrategyId.MEAN_REVERSION:
        return "均值回归，价格偏离后尝试反向回补。"
    return "风险暂停信号，当前不执行新开仓。"


def _is_stronger_replacement(
    current: ApprovedStrategyBinding | None,
    candidate: ApprovedStrategyBinding,
) -> bool:
    return is_stronger_strategy_replacement(current, candidate)


def _build_strategy_handover_events(
    symbol: str,
    current_strategy: ApprovedStrategyBinding | None,
    candidate_strategy: ApprovedStrategyBinding,
) -> list[dict[str, object]]:
    current_strategy_def_id = getattr(current_strategy, "strategy_def_id", None)
    current_strategy_package_id = getattr(current_strategy, "strategy_package_id", None)
    return [
        {
            "event_type": event_type,
            "symbol": symbol,
            "current_strategy_def_id": current_strategy_def_id,
            "current_strategy_package_id": current_strategy_package_id,
            "next_strategy_def_id": candidate_strategy.strategy_def_id,
            "next_strategy_package_id": candidate_strategy.strategy_package_id,
        }
        for event_type in build_strategy_handover_event_order()
    ]


def _can_relax_to_snapshot_mode(runtime: TraderRuntime, snapshot: StrategyConfigSnapshot) -> bool:
    return (
        _RUN_MODE_PRIORITY[runtime.current_mode] > _RUN_MODE_PRIORITY[snapshot.market_mode]
        and snapshot.approval_state == ApprovalState.APPROVED
        and snapshot.market_mode != RunMode.HALTED
        and runtime.components.checkpoint_service.can_open_new_risk(runtime.startup_checkpoint)
        and not runtime.components.state_engine.fault_flags
    )


async def _evaluate_symbol(runtime: TraderRuntime, symbol: str) -> None:
    latest_snapshot = runtime.snapshot_store.get_latest_snapshot()
    if latest_snapshot is not None:
        runtime.startup_snapshot = latest_snapshot
        if _can_relax_to_snapshot_mode(runtime, latest_snapshot):
            runtime.current_mode = latest_snapshot.market_mode
            runtime.components.state_engine.set_run_mode(runtime.current_mode)
            runtime.opening_allowed = True
            _publish_runtime_state(runtime)
    if runtime.startup_snapshot.version_id == "bootstrap":
        return
    snapshot = runtime.components.state_engine.snapshot(symbol)
    signals = build_candidate_signals(snapshot)
    open_orders = runtime.components.state_engine.open_orders_by_symbol.get(symbol, {})
    position = runtime.components.state_engine.positions_by_symbol.get(symbol)
    has_exposure = bool(open_orders) or (position is not None and position.net_quantity != 0.0)
    for signal in signals:
        decision = runtime.components.risk_kernel.evaluate(signal, runtime.startup_snapshot)
        if signal.entry_type != EntryType.MARKET or signal.side not in {OrderSide.BUY, OrderSide.SELL}:
            continue
        if has_exposure:
            continue
        try:
            response = await runtime.execution_coordinator.submit_market_open(
                symbol=signal.symbol,
                side=signal.side.value,
                size=max(1.0, decision.max_order_size),
                client_order_id=_next_client_order_id(runtime, signal),
                decision=decision,
                timestamp=_now_timestamp(),
            )
        except Exception as exc:
            runtime.history_store.append_risk_event(
                {
                    "event_type": "execution_submission_failed",
                    "symbol": signal.symbol,
                    "detail": str(exc),
                }
            )
            continue
        if response is None:
            runtime.history_store.append_risk_event(
                {
                    "event_type": "signal_blocked",
                    "symbol": signal.symbol,
                    "detail": ",".join(decision.reason_codes),
                }
            )
            continue
        for row in response:
            runtime.history_store.append_order_fact(
                {
                    "symbol": signal.symbol,
                    "side": signal.side.value,
                    "status": "submitted",
                    "client_order_id": str(row.get("clOrdId") or ""),
                    "order_id": str(row.get("ordId") or ""),
                    "intent": "open",
                    "strategy_id": signal.strategy_id.value,
                    "strategy_logic": _build_strategy_logic(signal),
                }
            )
        runtime.components.state_engine.stage_order_submission(
            signal.symbol,
            client_order_id=str(response[0].get("clOrdId") or ""),
            side=signal.side.value,
            size=max(1.0, decision.max_order_size),
            intent="open",
            strategy_id=signal.strategy_id.value,
            strategy_logic=_build_strategy_logic(signal),
        )
        _LOGGER.info(
            "order_submitted",
            extra={
                "service": "trader",
                "symbol": signal.symbol,
                "strategy_id": signal.strategy_id.value,
                "side": signal.side.value,
                "client_order_id": response[0].get("clOrdId") or "",
                "run_mode": runtime.current_mode.value,
            },
        )
        has_exposure = True


async def _consume_stream(runtime: TraderRuntime, adapter: object, **kwargs: object) -> bool:
    iterator_factory = getattr(adapter, "iter_events", None)
    if not callable(iterator_factory):
        return False
    consumed_any = False
    try:
        async for event in iterator_factory(**kwargs):
            consumed_any = True
            await _dispatch_runtime_event(runtime, event)
    except Exception as exc:
        await _dispatch_runtime_event(
            runtime,
            FaultEvent(
                event_type=TraderEventType.RUNTIME_FAULT,
                exchange="okx",
                generated_at=datetime.now(UTC),
                severity="critical",
                code=f"{adapter.__class__.__name__.lower()}_stream_failed",
                detail=str(exc),
            ),
        )
        return False
    return consumed_any


async def _dispatch_runtime_event(runtime: TraderRuntime, event: object) -> None:
    dispatch_event(runtime.components.state_engine, event)
    symbol = getattr(event, "symbol", None)
    if isinstance(event, (OrderbookTopEvent, MarketTradeEvent)) and symbol is not None:
        _publish_runtime_state(runtime, symbol=symbol)
        await _evaluate_symbol(runtime, symbol)
        return
    if isinstance(event, OrderUpdateEvent):
        order_state = runtime.components.state_engine.open_orders_by_symbol.get(event.symbol, {}).get(event.order_id)
        if order_state is None:
            for candidate in runtime.components.state_engine.open_orders_by_symbol.get(event.symbol, {}).values():
                if candidate.client_order_id == event.client_order_id:
                    order_state = candidate
                    break
        runtime.history_store.append_order_fact(
            {
                "symbol": event.symbol,
                "side": event.side,
                "status": event.status.strip().lower(),
                "client_order_id": event.client_order_id,
                "order_id": event.order_id,
                "filled_size": event.filled_size,
                "intent": getattr(order_state, "intent", None),
                "strategy_id": getattr(order_state, "strategy_id", None),
                "strategy_logic": getattr(order_state, "strategy_logic", None),
            }
        )
        if event.filled_size > 0:
            runtime.history_store.append_fill_fact(
                {
                    "symbol": event.symbol,
                    "side": event.side,
                    "client_order_id": event.client_order_id,
                    "order_id": event.order_id,
                    "filled_size": event.filled_size,
                }
            )
    elif isinstance(event, PositionUpdateEvent):
        trade_context = runtime.components.state_engine.trade_context_by_symbol.get(event.symbol, {})
        runtime.history_store.append_position_fact(
            {
                "symbol": event.symbol,
                "net_quantity": event.net_quantity,
                "average_price": event.average_price,
                "mark_price": event.mark_price,
                "unrealized_pnl": event.unrealized_pnl,
                **trade_context,
            }
        )
    elif isinstance(event, FaultEvent):
        _tighten_runtime_mode(runtime, _fault_mode(event))
        runtime.history_store.append_risk_event(
            {
                "event_type": "runtime_fault",
                "symbol": "system",
                "detail": f"{event.code}: {event.detail}",
            }
        )
    _publish_runtime_state(runtime, symbol=symbol)
    if isinstance(event, (OrderUpdateEvent, PositionUpdateEvent, AccountSnapshotEvent)):
        runtime.startup_checkpoint = _build_execution_checkpoint(runtime, checkpoint_id="runtime")
        runtime.history_store.save_checkpoint(runtime.startup_checkpoint.model_dump(mode="json"))


async def _run_trader(runtime: TraderRuntime) -> None:
    _LOGGER.info(
        "runtime_started",
        extra={
            "service": "trader",
            "mode": runtime.current_mode.value,
            "symbols": list(runtime.settings.okx_symbols),
        },
    )
    latest_snapshot = runtime.snapshot_store.get_latest_snapshot()
    if latest_snapshot is not None:
        runtime.startup_snapshot = latest_snapshot
    runtime.startup_checkpoint = _load_latest_checkpoint(runtime)
    runtime.startup_checkpoint.active_snapshot_version = runtime.startup_snapshot.version_id
    if runtime.startup_checkpoint.checkpoint_id != "startup":
        recovery_timestamp = _now_timestamp()
        recovery_results = [
            await runtime.recovery_supervisor.run_startup_recovery(symbol, runtime.startup_checkpoint, recovery_timestamp)
            for symbol in runtime.settings.okx_symbols
        ]
        for result in recovery_results:
            result_mode = RunMode(str(result["run_mode"]))
            _tighten_runtime_mode(runtime, result_mode)
            if bool(result.get("needs_reconcile")):
                runtime.opening_allowed = False
                runtime.startup_checkpoint.needs_reconcile = True
                runtime.history_store.append_risk_event(
                    {
                        "event_type": "startup_recovery_failed",
                        "symbol": "system",
                        "detail": str(result.get("reason") or "exchange_state_mismatch"),
                    }
                )
            else:
                runtime.startup_checkpoint.needs_reconcile = False
    runtime.opening_allowed = runtime.opening_allowed and runtime.components.checkpoint_service.can_open_new_risk(
        runtime.startup_checkpoint
    )
    if _can_relax_to_snapshot_mode(runtime, runtime.startup_snapshot):
        runtime.current_mode = runtime.startup_snapshot.market_mode
        runtime.opening_allowed = True
    else:
        runtime.current_mode = _more_restrictive_mode(runtime.current_mode, runtime.startup_snapshot.market_mode)
    if not runtime.opening_allowed:
        runtime.current_mode = _more_restrictive_mode(runtime.current_mode, RunMode.REDUCE_ONLY)
    if _can_relax_to_snapshot_mode(runtime, runtime.startup_snapshot):
        runtime.current_mode = runtime.startup_snapshot.market_mode
        runtime.opening_allowed = True
    runtime.components.state_engine.set_run_mode(runtime.current_mode)
    runtime.startup_snapshot = runtime.startup_snapshot.model_copy(update={"market_mode": runtime.current_mode})
    runtime.startup_checkpoint.current_mode = runtime.current_mode
    _publish_runtime_state(runtime)
    runtime.startup_checkpoint = _build_execution_checkpoint(runtime, checkpoint_id=runtime.startup_checkpoint.checkpoint_id)
    runtime.history_store.save_checkpoint(runtime.startup_checkpoint.model_dump(mode="json"))
    if not runtime.opening_allowed:
        runtime.history_store.append_risk_event(
            {
                "event_type": "runtime_mode_changed",
                "symbol": "system",
                "detail": f"startup gating tightened runtime to {runtime.current_mode.value}",
            }
        )
    consume_tasks = [
        asyncio.create_task(
            _consume_stream(
                runtime,
                runtime.components.okx_public_stream,
                symbols=runtime.settings.okx_symbols,
            )
        ),
        asyncio.create_task(
            _consume_stream(
                runtime,
                runtime.components.okx_private_stream,
                symbols=runtime.settings.okx_symbols,
                api_key=runtime.settings.okx_api_key.get_secret_value(),
                api_secret=runtime.settings.okx_api_secret.get_secret_value(),
                passphrase=runtime.settings.okx_api_passphrase.get_secret_value(),
            )
        ),
    ]
    consumed_public, consumed_private = await asyncio.gather(*consume_tasks)
    if consumed_public or consumed_private:
        return
    await _wait_forever()


def main() -> int:
    async def _main() -> None:
        runtime = build_trader_runtime()
        try:
            await _run_trader(runtime)
        finally:
            await runtime.components.aclose()

    try:
        asyncio.run(_main())
    except Exception as exc:
        _LOGGER.exception("runtime_failed", extra={"service": "trader", "error": str(exc)})
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

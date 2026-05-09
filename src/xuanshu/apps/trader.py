from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re

from xuanshu.checkpoints.service import CheckpointService
from xuanshu.config.settings import TraderRuntimeSettings
from xuanshu.core.enums import (
    ApprovalState,
    EntryType,
    OkxAccountMode,
    OrderSide,
    RunMode,
    SignalUrgency,
    StrategyId,
    TraderEventType,
)
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
from xuanshu.infra.okx.rest import OkxBusinessError, OkxRestClient
from xuanshu.infra.storage.postgres_store import PostgresRuntimeStore
from xuanshu.infra.storage.redis_store import (
    RedisRuntimeStateStore,
    RedisSnapshotStore,
    RuntimeStateStore,
    SnapshotStore,
)
from xuanshu.risk.kernel import RiskKernel
from xuanshu.sizing.position_sizer import OpenOrderSizingInput, calculate_open_order_size
from xuanshu.state.engine import StateEngine
from xuanshu.strategies.signals import build_candidate_signals
from xuanshu.ops.runtime_logging import configure_runtime_logger
from xuanshu.trader.dispatcher import build_strategy_handover_event_order, dispatch_event
from xuanshu.trader.recovery import RecoverySupervisor
from xuanshu.vote_trend.backtest import VoteTrendParameters, latest_vote_trend_side
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
_SHORT_MOMENTUM_EXIT_PATTERN = re.compile(r"(?:^|-)sl(?P<stop>\d+)-tp(?P<take>\d+)-h(?P<hold>\d+)(?:-|$)")
_FIXED_VOL_BREAKOUT_PATTERN = re.compile(
    r"(?:^|-)vol-breakout-.+-(?P<bar>\d+[mhd])-k(?P<k>\d+)-ta(?P<trailing>\d+)-h(?P<hold>\d+)-atr(?P<atr>\d+)-ema(?P<ema>\d+)(?:-|$)",
    re.IGNORECASE,
)
_FIXED_VOTE_TREND_PATTERN = re.compile(
    r"(?:^|-)vote-trend-.+-(?P<bar>\d+[mhd])-f(?P<fast>\d+)-s(?P<slow>\d+)"
    r"-lb(?P<lookback>\d+)-ch(?P<channel>\d+)-th(?P<threshold>\d+)-v(?P<votes>\d+)"
    r"-sl(?P<stop>\d+)-tp(?P<take>\d+)-h(?P<hold>\d+)(?:-(?P<mode>both|longonly))?(?:-|$)",
    re.IGNORECASE,
)
_BAR_PATTERN = re.compile(r"(?P<count>\d+)(?P<unit>[mhd])", re.IGNORECASE)
_DEFAULT_VOL_BREAKOUT_STOP_LOSS_BPS = 300
_DEFAULT_VOL_BREAKOUT_TAKE_PROFIT_BPS = 800
_DEFAULT_VOL_BREAKOUT_TRAILING_DRAWDOWN_BPS = 250
_DEFAULT_VOL_BREAKOUT_MAX_HOLD_BARS = 12
_LONG_ENTRY_CONFIRMATION_TICKS = 2
_OPEN_EXECUTION_FAILURE_COOLDOWN = timedelta(minutes=15)
_FIXED_VOL_BREAKOUT_CACHE_MIN_TTL = timedelta(seconds=15)
_FIXED_VOL_BREAKOUT_CACHE_MAX_TTL = timedelta(seconds=30)
_FIXED_VOTE_TREND_CACHE_MIN_TTL = timedelta(seconds=30)
_FIXED_VOTE_TREND_CACHE_MAX_TTL = timedelta(minutes=5)
_PUBLIC_STREAM_RECONNECT_MARKERS = (
    "closed",
    "connection closed",
    "connection reset",
    "going away",
    "timeout",
    "timed out",
    "keepalive ping timeout",
    "no close frame",
)
_PUBLIC_STREAM_FAULT_CODES = frozenset(
    {
        "okxpublicstream_stream_failed",
        "public_ws_disconnected",
        "public_ws_error",
        "public_ws_malformed_envelope",
        "public_ws_unknown_channel",
    }
)
_PRIVATE_STREAM_FAULT_CODES = frozenset(
    {
        "okxprivatestream_stream_failed",
        "private_ws_disconnected",
    }
)


@dataclass(slots=True)
class PositionEntryContext:
    symbol: str
    position_side: str
    quantity: float
    entry_price: float
    entered_at: datetime
    strategy_id: str
    strategy_logic: str
    highest_mark_price: float
    lowest_mark_price: float


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
    position_entry_contexts: dict[str, PositionEntryContext] = field(default_factory=dict)
    pending_position_actions: dict[str, str] = field(default_factory=dict)
    pending_reverse_signals: dict[str, CandidateSignal] = field(default_factory=dict)
    long_entry_confirmations: dict[str, int] = field(default_factory=dict)
    open_execution_failure_cooldowns: dict[str, datetime] = field(default_factory=dict)
    vol_breakout_signal_candles: dict[str, str] = field(default_factory=dict)
    vol_breakout_entry_candles: dict[str, str] = field(default_factory=dict)
    vol_breakout_rows_cache: dict[str, tuple[datetime, list[dict[str, object]]]] = field(default_factory=dict)
    vol_breakout_history_fetch_cooldowns: dict[str, datetime] = field(default_factory=dict)
    vote_trend_signal_candles: dict[str, str] = field(default_factory=dict)
    vote_trend_entry_candles: dict[str, str] = field(default_factory=dict)
    vote_trend_rows_cache: dict[str, tuple[datetime, list[dict[str, object]]]] = field(default_factory=dict)
    vote_trend_history_fetch_cooldowns: dict[str, datetime] = field(default_factory=dict)
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
        strategy_enable_flags={"vol_breakout": True, "risk_pause": True},
        risk_multiplier=0.5,
        per_symbol_max_position=0.12,
        max_leverage=3,
        market_mode=settings.default_run_mode,
        approval_state=ApprovalState.APPROVED,
        source_reason="bootstrap",
        ttl_sec=300,
    )


def _load_fixed_strategy_snapshot(path: str | None) -> StrategyConfigSnapshot | None:
    if path is None or not path.strip():
        return None
    snapshot_path = Path(path)
    try:
        payload = snapshot_path.read_text(encoding="utf-8")
        return StrategyConfigSnapshot.model_validate_json(payload)
    except Exception as exc:
        raise ValueError(f"invalid fixed strategy snapshot: {snapshot_path}") from exc


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


def _uses_fixed_strategy_snapshot(runtime: TraderRuntime) -> bool:
    path = runtime.settings.fixed_strategy_snapshot_path
    return path is not None and bool(path.strip())


def _snapshot_strategy_bindings(snapshot: StrategyConfigSnapshot) -> dict[str, ApprovedStrategyBinding]:
    bindings = {
        symbol: binding
        for symbol, binding in snapshot.symbol_strategy_bindings.items()
    }
    bindings.update(snapshot.strategy_bindings)
    return bindings


def build_trader_runtime() -> TraderRuntime:
    settings = TraderRuntimeSettings()
    components = build_trader_components(settings)
    snapshot_store = build_snapshot_store(settings)
    startup_snapshot = _build_startup_snapshot(settings)
    fixed_snapshot = _load_fixed_strategy_snapshot(settings.fixed_strategy_snapshot_path)
    if fixed_snapshot is not None:
        startup_snapshot = fixed_snapshot.model_copy(
            update={
                "market_mode": _more_restrictive_mode(
                    settings.default_run_mode,
                    fixed_snapshot.market_mode,
                )
            }
        )
    else:
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
        active_symbol_strategies=_snapshot_strategy_bindings(startup_snapshot),
        current_mode=initial_mode,
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


def _now_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _build_runtime_budget_summary(runtime: TraderRuntime) -> dict[str, object]:
    synced_strategy_total = _account_synced_strategy_total(runtime)
    summary = {
        "max_daily_loss": runtime.startup_checkpoint.budget_state.max_daily_loss,
        "remaining_daily_loss": runtime.startup_checkpoint.budget_state.remaining_daily_loss,
        "remaining_notional": runtime.startup_checkpoint.budget_state.remaining_notional,
        "remaining_order_count": runtime.startup_checkpoint.budget_state.remaining_order_count,
        "current_mode": runtime.current_mode.value,
        "starting_nav": runtime.starting_nav,
        "strategy_total_amount": synced_strategy_total or runtime.components.risk_kernel.nav,
        **runtime.components.state_engine.build_budget_pool_summary(),
    }
    existing = runtime.runtime_store.get_budget_pool_summary()
    if (
        synced_strategy_total is None
        and isinstance(existing, dict)
        and existing.get("manual_strategy_total_amount_override") is True
    ):
        if "strategy_total_amount" in existing:
            summary["strategy_total_amount"] = existing["strategy_total_amount"]
        summary["manual_strategy_total_amount_override"] = True
    return summary


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
        checkpoint = ExecutionCheckpoint.model_validate(latest_row)
    except Exception:
        return runtime.startup_checkpoint
    if checkpoint.active_snapshot_version != runtime.startup_snapshot.version_id:
        return _build_startup_checkpoint(runtime.startup_snapshot, runtime.current_mode)
    return checkpoint


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


def _relax_runtime_mode(runtime: TraderRuntime, target_mode: RunMode, *, detail: str) -> None:
    previous_mode = runtime.current_mode
    runtime.current_mode = target_mode
    runtime.components.state_engine.set_run_mode(target_mode)
    runtime.startup_checkpoint.current_mode = target_mode
    runtime.opening_allowed = target_mode not in {RunMode.REDUCE_ONLY, RunMode.HALTED}
    if target_mode != previous_mode:
        _LOGGER.info(
            "runtime_mode_changed",
            extra={
                "service": "trader",
                "previous_mode": previous_mode.value,
                "current_mode": target_mode.value,
            },
        )
        runtime.history_store.append_risk_event(
            {
                "event_type": "runtime_mode_changed",
                "symbol": "system",
                "detail": detail,
            }
        )


def _fault_mode(event: FaultEvent) -> RunMode:
    if "public" in event.code or event.severity == "warn":
        return RunMode.DEGRADED
    if "private" in event.code or event.severity == "critical":
        return RunMode.REDUCE_ONLY
    return RunMode.NORMAL


def _clear_recovered_stream_faults(runtime: TraderRuntime, event: object) -> tuple[str, ...]:
    if isinstance(event, (OrderbookTopEvent, MarketTradeEvent)):
        recoverable_codes = _PUBLIC_STREAM_FAULT_CODES
    elif isinstance(event, (OrderUpdateEvent, PositionUpdateEvent, AccountSnapshotEvent)):
        recoverable_codes = _PRIVATE_STREAM_FAULT_CODES
    else:
        return ()

    fault_flags = runtime.components.state_engine.fault_flags
    cleared = tuple(code for code in recoverable_codes if code in fault_flags)
    for code in cleared:
        fault_flags.pop(code, None)
    return cleared


def _try_recover_runtime_after_stream_event(runtime: TraderRuntime, event: object) -> None:
    cleared_codes = _clear_recovered_stream_faults(runtime, event)
    if not cleared_codes:
        return
    runtime.history_store.append_risk_event(
        {
            "event_type": "runtime_fault_recovered",
            "symbol": "system",
            "detail": ",".join(sorted(cleared_codes)),
        }
    )
    if _can_relax_to_snapshot_mode(runtime, runtime.startup_snapshot):
        _relax_runtime_mode(
            runtime,
            runtime.startup_snapshot.market_mode,
            detail=(
                "stream fault recovered; "
                f"released {runtime.current_mode.value} to {runtime.startup_snapshot.market_mode.value}"
            ),
        )


def _next_client_order_id(runtime: TraderRuntime, signal: CandidateSignal) -> str:
    runtime.execution_sequence += 1
    return runtime.components.client_order_id_builder(
        signal.symbol,
        signal.strategy_id.value,
        runtime.execution_sequence,
    )


def _is_short_priority_long_close(signal: CandidateSignal, position: object | None) -> bool:
    position_quantity = getattr(position, "net_quantity", 0.0) if position is not None else 0.0
    position_side = getattr(position, "position_side", "long") if position is not None else "long"
    return (
        signal.strategy_id == StrategyId.SHORT_MOMENTUM
        and signal.side == OrderSide.SELL
        and position_quantity > 0.0
        and position_side == "long"
    )


def _position_side_for_signal(signal: CandidateSignal) -> str | None:
    if signal.side == OrderSide.BUY:
        return "long"
    if signal.side == OrderSide.SELL:
        return "short"
    return None


def _closing_order_side(position_side: str) -> OrderSide:
    return OrderSide.SELL if position_side == "long" else OrderSide.BUY


def _snapshot_uses_vote_trend(snapshot: StrategyConfigSnapshot, symbol: str) -> bool:
    return (
        snapshot.is_strategy_enabled(StrategyId.VOTE_TREND.value)
        and _parse_fixed_vote_trend_parameters(snapshot.strategy_binding_for(symbol, StrategyId.VOTE_TREND.value))
        is not None
    )


def _strategy_id_for_position_side(
    position_side: str,
    *,
    snapshot: StrategyConfigSnapshot | None = None,
    symbol: str | None = None,
) -> StrategyId:
    if snapshot is not None and symbol is not None and _snapshot_uses_vote_trend(snapshot, symbol):
        return StrategyId.VOTE_TREND
    return StrategyId.VOL_BREAKOUT if position_side == "long" else StrategyId.SHORT_MOMENTUM


def _binding_for_position_side(
    snapshot: StrategyConfigSnapshot,
    symbol: str,
    position_side: str,
) -> ApprovedStrategyBinding | None:
    strategy_id = _strategy_id_for_position_side(position_side, snapshot=snapshot, symbol=symbol)
    return snapshot.strategy_binding_for(symbol, strategy_id.value)


def _extract_short_momentum_exit_rules(binding: ApprovedStrategyBinding | None) -> tuple[int, int, int] | None:
    if binding is None:
        return None
    match = _SHORT_MOMENTUM_EXIT_PATTERN.search(binding.strategy_def_id)
    if match is None:
        return None
    return int(match.group("stop")), int(match.group("take")), int(match.group("hold"))


def _extract_bar_duration(strategy_def_id: object) -> timedelta:
    for part in str(strategy_def_id or "").split("-"):
        match = _BAR_PATTERN.fullmatch(part)
        if match is None:
            continue
        count = int(match.group("count"))
        unit = match.group("unit").lower()
        if unit == "m":
            return timedelta(minutes=count)
        if unit == "h":
            return timedelta(hours=count)
        if unit == "d":
            return timedelta(days=count)
    return timedelta(hours=4)


@dataclass(frozen=True, slots=True)
class FixedVolBreakoutParameters:
    bar: str
    k: float
    trailing_atr: float
    max_hold_bars: int
    atr_period: int
    ema_period: int


@dataclass(frozen=True, slots=True)
class FixedVoteTrendParameters:
    bar: str
    fast_ema_period: int
    slow_ema_period: int
    lookback_bars: int
    channel_bars: int
    threshold_bps: int
    required_votes: int
    stop_loss_bps: int
    take_profit_bps: int
    max_hold_bars: int
    allow_short: bool

    def to_vote_trend_parameters(self) -> VoteTrendParameters:
        return VoteTrendParameters(
            fast_ema_period=self.fast_ema_period,
            slow_ema_period=self.slow_ema_period,
            lookback_bars=self.lookback_bars,
            channel_bars=self.channel_bars,
            threshold_bps=self.threshold_bps,
            required_votes=self.required_votes,
            stop_loss_bps=self.stop_loss_bps,
            take_profit_bps=self.take_profit_bps,
            max_hold_bars=self.max_hold_bars,
            allow_short=self.allow_short,
        )


def _decode_compact_decimal(value: str) -> float:
    if len(value) <= 1:
        return float(value)
    if value.startswith("0"):
        return int(value) / 10
    if len(value) == 2:
        return int(value) / 10
    return int(value) / 100


def _parse_fixed_vol_breakout_parameters(binding: ApprovedStrategyBinding | None) -> FixedVolBreakoutParameters | None:
    if binding is None:
        return None
    match = _FIXED_VOL_BREAKOUT_PATTERN.search(binding.strategy_def_id)
    if match is None:
        return None
    return FixedVolBreakoutParameters(
        bar=match.group("bar").upper(),
        k=_decode_compact_decimal(match.group("k")),
        trailing_atr=_decode_compact_decimal(match.group("trailing")),
        max_hold_bars=int(match.group("hold")),
        atr_period=int(match.group("atr")),
        ema_period=int(match.group("ema")),
    )


def _parse_fixed_vote_trend_parameters(binding: ApprovedStrategyBinding | None) -> FixedVoteTrendParameters | None:
    if binding is None:
        return None
    match = _FIXED_VOTE_TREND_PATTERN.search(binding.strategy_def_id)
    if match is None:
        return None
    return FixedVoteTrendParameters(
        bar=match.group("bar").upper(),
        fast_ema_period=int(match.group("fast")),
        slow_ema_period=int(match.group("slow")),
        lookback_bars=int(match.group("lookback")),
        channel_bars=int(match.group("channel")),
        threshold_bps=int(match.group("threshold")),
        required_votes=int(match.group("votes")),
        stop_loss_bps=int(match.group("stop")),
        take_profit_bps=int(match.group("take")),
        max_hold_bars=int(match.group("hold")),
        allow_short=(match.group("mode") or "both").lower() != "longonly",
    )


def _default_vol_breakout_exit_reason(
    *,
    position: object,
    context: PositionEntryContext | None,
) -> str | None:
    active_return = _active_position_return(position, "long")
    if active_return is None:
        return None
    if active_return <= -(_DEFAULT_VOL_BREAKOUT_STOP_LOSS_BPS / 10_000):
        return f"vol_breakout_stop_loss_{_DEFAULT_VOL_BREAKOUT_STOP_LOSS_BPS}bps"
    if active_return >= _DEFAULT_VOL_BREAKOUT_TAKE_PROFIT_BPS / 10_000:
        return f"vol_breakout_take_profit_{_DEFAULT_VOL_BREAKOUT_TAKE_PROFIT_BPS}bps"
    if context is None:
        return None
    mark_price = float(getattr(position, "mark_price", 0.0) or 0.0)
    if context.highest_mark_price > 0.0 and mark_price > 0.0:
        drawdown = (context.highest_mark_price - mark_price) / context.highest_mark_price
        if drawdown >= _DEFAULT_VOL_BREAKOUT_TRAILING_DRAWDOWN_BPS / 10_000:
            return f"vol_breakout_trailing_drawdown_{_DEFAULT_VOL_BREAKOUT_TRAILING_DRAWDOWN_BPS}bps"
    max_hold = timedelta(hours=4) * _DEFAULT_VOL_BREAKOUT_MAX_HOLD_BARS
    if datetime.now(UTC) - context.entered_at >= max_hold:
        return f"vol_breakout_max_hold_{_DEFAULT_VOL_BREAKOUT_MAX_HOLD_BARS}bars"
    return None


def _active_position_return(position: object, position_side: str) -> float | None:
    average_price = getattr(position, "average_price", 0.0)
    mark_price = getattr(position, "mark_price", 0.0)
    try:
        average = float(average_price)
        mark = float(mark_price)
    except (TypeError, ValueError):
        return None
    if average <= 0.0 or mark <= 0.0:
        return None
    if position_side == "short":
        return (average / mark) - 1.0
    return (mark / average) - 1.0


def _linear_active_position_return(position: object, position_side: str) -> float | None:
    average_price = getattr(position, "average_price", 0.0)
    mark_price = getattr(position, "mark_price", 0.0)
    try:
        average = float(average_price)
        mark = float(mark_price)
    except (TypeError, ValueError):
        return None
    if average <= 0.0 or mark <= 0.0:
        return None
    if position_side == "short":
        return (average - mark) / average
    return (mark / average) - 1.0


async def _fixed_vol_breakout_exit_reason(
    runtime: TraderRuntime,
    *,
    symbol: str,
    binding: ApprovedStrategyBinding,
    position: object,
    context: PositionEntryContext | None,
) -> str | None:
    parameters = _parse_fixed_vol_breakout_parameters(binding)
    if parameters is None:
        return _default_vol_breakout_exit_reason(position=position, context=context)
    if context is None:
        return None
    rows = await _get_fixed_vol_breakout_rows(
        runtime,
        symbol=symbol,
        parameters=parameters,
        failure_detail_prefix="fixed_vol_breakout_exit_history_fetch_failed",
    )
    if rows is None:
        return None
    warmup = max(parameters.ema_period, parameters.atr_period + 1)
    if len(rows) <= warmup:
        return None
    closes = [float(row["close"]) for row in rows]
    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]
    atr_values = _atr(highs=highs, lows=lows, closes=closes, period=parameters.atr_period)
    latest_close = closes[-1]
    recent_closes = [
        float(row["close"])
        for row in rows
        if isinstance(row.get("timestamp"), datetime) and row["timestamp"] >= context.entered_at
    ]
    highest_close = max([context.entry_price, *recent_closes]) if recent_closes else context.entry_price
    trailing_stop = highest_close - parameters.trailing_atr * atr_values[-1]
    if latest_close < trailing_stop:
        return f"vol_breakout_trailing_atr_{parameters.trailing_atr:g}x"
    max_hold = _extract_bar_duration(parameters.bar) * parameters.max_hold_bars
    if datetime.now(UTC) - context.entered_at >= max_hold:
        return f"vol_breakout_max_hold_{parameters.max_hold_bars}bars"
    return None


def _fixed_vote_trend_exit_reason(
    *,
    parameters: FixedVoteTrendParameters,
    position: object,
    position_side: str,
    context: PositionEntryContext | None,
) -> str | None:
    active_return = _linear_active_position_return(position, position_side)
    if active_return is None:
        return None
    if active_return <= -(parameters.stop_loss_bps / 10_000):
        return f"vote_trend_stop_loss_{parameters.stop_loss_bps}bps"
    if active_return >= parameters.take_profit_bps / 10_000:
        return f"vote_trend_take_profit_{parameters.take_profit_bps}bps"
    if context is not None:
        max_hold = _extract_bar_duration(parameters.bar) * parameters.max_hold_bars
        if datetime.now(UTC) - context.entered_at >= max_hold:
            return f"vote_trend_max_hold_{parameters.max_hold_bars}bars"
    return None


async def _position_exit_reason(runtime: TraderRuntime, symbol: str, position: object) -> str | None:
    position_side = str(getattr(position, "position_side", "long") or "long")
    position_quantity = float(getattr(position, "net_quantity", 0.0) or 0.0)
    if position_quantity == 0.0:
        return None
    binding = _binding_for_position_side(runtime.startup_snapshot, symbol, position_side)
    context = runtime.position_entry_contexts.get(symbol)
    vote_trend_parameters = _parse_fixed_vote_trend_parameters(binding)
    if vote_trend_parameters is not None:
        return _fixed_vote_trend_exit_reason(
            parameters=vote_trend_parameters,
            position=position,
            position_side=position_side,
            context=context,
        )
    if position_side == "long":
        if binding is not None:
            return await _fixed_vol_breakout_exit_reason(
                runtime,
                symbol=symbol,
                binding=binding,
                position=position,
                context=context,
            )
        return _default_vol_breakout_exit_reason(position=position, context=context)
    if position_side == "short":
        rules = _extract_short_momentum_exit_rules(binding)
        active_return = _active_position_return(position, position_side)
        if rules is not None and active_return is not None:
            stop_loss_bps, take_profit_bps, max_hold_hours = rules
            if active_return <= -(stop_loss_bps / 10_000):
                return f"short_momentum_stop_loss_{stop_loss_bps}bps"
            if active_return >= take_profit_bps / 10_000:
                return f"short_momentum_take_profit_{take_profit_bps}bps"
            if context is not None and datetime.now(UTC) - context.entered_at >= timedelta(hours=max_hold_hours):
                return f"short_momentum_max_hold_{max_hold_hours}h"
    return None


def _opposite_signal(
    position_side: str,
    signals: list[CandidateSignal],
    snapshot: StrategyConfigSnapshot,
) -> CandidateSignal | None:
    for signal in signals:
        signal_position_side = _position_side_for_signal(signal)
        if (
            signal.entry_type == EntryType.MARKET
            and signal_position_side is not None
            and signal_position_side != position_side
            and signal.strategy_id == StrategyId.VOTE_TREND
            and snapshot.is_strategy_enabled(signal.strategy_id.value)
        ):
            return signal
        if (
            signal.entry_type == EntryType.MARKET
            and signal_position_side is not None
            and signal_position_side != position_side
            and signal.strategy_id == StrategyId.SHORT_MOMENTUM
            and snapshot.is_strategy_enabled(signal.strategy_id.value)
        ):
            return signal
    return None


def _prioritize_strategy_signals(signals: list[CandidateSignal]) -> list[CandidateSignal]:
    priority = {
        StrategyId.VOTE_TREND: 0,
        StrategyId.SHORT_MOMENTUM: 1,
        StrategyId.VOL_BREAKOUT: 2,
    }
    return sorted(signals, key=lambda signal: priority.get(signal.strategy_id, 10))


def _enabled_candidate_signals(
    signals: list[CandidateSignal],
    snapshot: StrategyConfigSnapshot,
) -> list[CandidateSignal]:
    return [
        signal
        for signal in signals
        if signal.strategy_id == StrategyId.RISK_PAUSE or snapshot.is_strategy_enabled(signal.strategy_id.value)
    ]


def _long_entry_confirmed(runtime: TraderRuntime, signal: CandidateSignal) -> bool:
    if signal.strategy_id not in {StrategyId.VOL_BREAKOUT, StrategyId.VOTE_TREND} or signal.side != OrderSide.BUY:
        runtime.long_entry_confirmations.pop(signal.symbol, None)
        return True
    if signal.symbol in runtime.vol_breakout_signal_candles or signal.symbol in runtime.vote_trend_signal_candles:
        runtime.long_entry_confirmations.pop(signal.symbol, None)
        return True
    confirmations = runtime.long_entry_confirmations.get(signal.symbol, 0) + 1
    runtime.long_entry_confirmations[signal.symbol] = confirmations
    return confirmations >= _LONG_ENTRY_CONFIRMATION_TICKS


def _open_failure_cooldown_key(signal: CandidateSignal) -> str:
    return f"{signal.symbol}:{signal.strategy_id.value}:{signal.side.value}:open"


def _is_open_execution_failure_cooling_down(runtime: TraderRuntime, signal: CandidateSignal) -> bool:
    cooldown_until = runtime.open_execution_failure_cooldowns.get(_open_failure_cooldown_key(signal))
    if cooldown_until is None:
        return False
    if datetime.now(UTC) < cooldown_until:
        return True
    runtime.open_execution_failure_cooldowns.pop(_open_failure_cooldown_key(signal), None)
    return False


def _start_open_execution_failure_cooldown(runtime: TraderRuntime, signal: CandidateSignal) -> str:
    cooldown_until = datetime.now(UTC) + _OPEN_EXECUTION_FAILURE_COOLDOWN
    runtime.open_execution_failure_cooldowns[_open_failure_cooldown_key(signal)] = cooldown_until
    runtime.long_entry_confirmations.pop(signal.symbol, None)
    return cooldown_until.isoformat()


def _format_execution_error(exc: Exception) -> str:
    if not isinstance(exc, OkxBusinessError):
        return str(exc)
    detail = {
        "code": exc.code,
        "message": exc.message,
        "payload": exc.payload,
    }
    return str(detail)


def _normalize_okx_candle(row: dict[str, object]) -> dict[str, object]:
    timestamp = row.get("ts")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise ValueError("OKX candle timestamp is missing")
    return {
        "timestamp": datetime.fromtimestamp(int(timestamp) / 1000, tz=UTC),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
    }


def _fixed_vol_breakout_rows_limit(parameters: FixedVolBreakoutParameters) -> int:
    warmup = max(parameters.ema_period, parameters.atr_period + 1)
    return min(max(warmup + 3, 120), 300)


def _fixed_vol_breakout_cache_key(symbol: str, parameters: FixedVolBreakoutParameters) -> str:
    return (
        f"{symbol}:{parameters.bar}:limit={_fixed_vol_breakout_rows_limit(parameters)}:"
        f"atr={parameters.atr_period}:ema={parameters.ema_period}"
    )


def _fixed_vol_breakout_cache_ttl(parameters: FixedVolBreakoutParameters) -> timedelta:
    bar_duration = _extract_bar_duration(parameters.bar)
    return min(
        max(bar_duration / 20, _FIXED_VOL_BREAKOUT_CACHE_MIN_TTL),
        _FIXED_VOL_BREAKOUT_CACHE_MAX_TTL,
    )


def _fixed_vol_breakout_failure_cooldown(parameters: FixedVolBreakoutParameters) -> timedelta:
    bar_duration = _extract_bar_duration(parameters.bar)
    return min(max(bar_duration / 10, timedelta(minutes=5)), timedelta(minutes=15))


async def _fetch_fixed_vol_breakout_rows(
    runtime: TraderRuntime,
    *,
    symbol: str,
    parameters: FixedVolBreakoutParameters,
) -> list[dict[str, object]]:
    limit = _fixed_vol_breakout_rows_limit(parameters)
    rows_by_timestamp: dict[datetime, dict[str, object]] = {}
    after: str | None = None
    remaining = limit
    while remaining > 0:
        request_limit = min(100, remaining)
        batch = await runtime.components.okx_rest_client.fetch_history_candles(
            symbol,
            bar=parameters.bar,
            after=after,
            limit=request_limit,
        )
        if not batch:
            break
        for item in batch:
            row = _normalize_okx_candle(item)
            rows_by_timestamp[row["timestamp"]] = row
        remaining = limit - len(rows_by_timestamp)
        oldest_ts = min(str(item["ts"]) for item in batch if "ts" in item)
        if after == oldest_ts or len(batch) < request_limit:
            break
        after = oldest_ts
    return [rows_by_timestamp[key] for key in sorted(rows_by_timestamp)]


async def _get_fixed_vol_breakout_rows(
    runtime: TraderRuntime,
    *,
    symbol: str,
    parameters: FixedVolBreakoutParameters,
    failure_detail_prefix: str,
) -> list[dict[str, object]] | None:
    now = datetime.now(UTC)
    cache_key = _fixed_vol_breakout_cache_key(symbol, parameters)
    cached = runtime.vol_breakout_rows_cache.get(cache_key)
    if cached is not None:
        expires_at, rows = cached
        if now < expires_at:
            return rows
        runtime.vol_breakout_rows_cache.pop(cache_key, None)

    failure_cooldown_until = runtime.vol_breakout_history_fetch_cooldowns.get(cache_key)
    if failure_cooldown_until is not None:
        if now < failure_cooldown_until:
            return None
        runtime.vol_breakout_history_fetch_cooldowns.pop(cache_key, None)

    try:
        rows = await _fetch_fixed_vol_breakout_rows(runtime, symbol=symbol, parameters=parameters)
    except Exception as exc:
        cooldown_until = now + _fixed_vol_breakout_failure_cooldown(parameters)
        runtime.vol_breakout_history_fetch_cooldowns[cache_key] = cooldown_until
        runtime.history_store.append_risk_event(
            {
                "event_type": "signal_blocked",
                "symbol": symbol,
                "detail": f"{failure_detail_prefix}: {_format_execution_error(exc)}",
                "strategy_id": StrategyId.VOL_BREAKOUT.value,
                "cooldown_until": cooldown_until.isoformat(),
            }
        )
        return None

    runtime.vol_breakout_rows_cache[cache_key] = (now + _fixed_vol_breakout_cache_ttl(parameters), rows)
    return rows


def _fixed_vote_trend_rows_limit(parameters: FixedVoteTrendParameters) -> int:
    warmup = max(parameters.slow_ema_period, parameters.lookback_bars, parameters.channel_bars, 14)
    return min(max(warmup + parameters.max_hold_bars + 3, 240), 300)


def _fixed_vote_trend_cache_key(symbol: str, parameters: FixedVoteTrendParameters) -> str:
    return (
        f"{symbol}:{parameters.bar}:limit={_fixed_vote_trend_rows_limit(parameters)}:"
        f"f={parameters.fast_ema_period}:s={parameters.slow_ema_period}:"
        f"lb={parameters.lookback_bars}:ch={parameters.channel_bars}:"
        f"th={parameters.threshold_bps}:v={parameters.required_votes}:short={parameters.allow_short}"
    )


def _fixed_vote_trend_cache_ttl(parameters: FixedVoteTrendParameters) -> timedelta:
    bar_duration = _extract_bar_duration(parameters.bar)
    return min(
        max(bar_duration / 20, _FIXED_VOTE_TREND_CACHE_MIN_TTL),
        _FIXED_VOTE_TREND_CACHE_MAX_TTL,
    )


def _fixed_vote_trend_failure_cooldown(parameters: FixedVoteTrendParameters) -> timedelta:
    bar_duration = _extract_bar_duration(parameters.bar)
    return min(max(bar_duration / 10, timedelta(minutes=5)), timedelta(minutes=15))


async def _fetch_fixed_vote_trend_rows(
    runtime: TraderRuntime,
    *,
    symbol: str,
    parameters: FixedVoteTrendParameters,
) -> list[dict[str, object]]:
    limit = _fixed_vote_trend_rows_limit(parameters)
    rows_by_timestamp: dict[datetime, dict[str, object]] = {}
    after: str | None = None
    remaining = limit
    while remaining > 0:
        request_limit = min(100, remaining)
        batch = await runtime.components.okx_rest_client.fetch_history_candles(
            symbol,
            bar=parameters.bar,
            after=after,
            limit=request_limit,
        )
        if not batch:
            break
        for item in batch:
            row = _normalize_okx_candle(item)
            rows_by_timestamp[row["timestamp"]] = row
        remaining = limit - len(rows_by_timestamp)
        oldest_ts = min(str(item["ts"]) for item in batch if "ts" in item)
        if after == oldest_ts or len(batch) < request_limit:
            break
        after = oldest_ts
    return [rows_by_timestamp[key] for key in sorted(rows_by_timestamp)]


async def _get_fixed_vote_trend_rows(
    runtime: TraderRuntime,
    *,
    symbol: str,
    parameters: FixedVoteTrendParameters,
    failure_detail_prefix: str,
) -> list[dict[str, object]] | None:
    now = datetime.now(UTC)
    cache_key = _fixed_vote_trend_cache_key(symbol, parameters)
    cached = runtime.vote_trend_rows_cache.get(cache_key)
    if cached is not None:
        expires_at, rows = cached
        if now < expires_at:
            return rows
        runtime.vote_trend_rows_cache.pop(cache_key, None)

    failure_cooldown_until = runtime.vote_trend_history_fetch_cooldowns.get(cache_key)
    if failure_cooldown_until is not None:
        if now < failure_cooldown_until:
            return None
        runtime.vote_trend_history_fetch_cooldowns.pop(cache_key, None)

    try:
        rows = await _fetch_fixed_vote_trend_rows(runtime, symbol=symbol, parameters=parameters)
    except Exception as exc:
        cooldown_until = now + _fixed_vote_trend_failure_cooldown(parameters)
        runtime.vote_trend_history_fetch_cooldowns[cache_key] = cooldown_until
        runtime.history_store.append_risk_event(
            {
                "event_type": "signal_blocked",
                "symbol": symbol,
                "detail": f"{failure_detail_prefix}: {_format_execution_error(exc)}",
                "strategy_id": StrategyId.VOTE_TREND.value,
                "cooldown_until": cooldown_until.isoformat(),
            }
        )
        return None

    runtime.vote_trend_rows_cache[cache_key] = (now + _fixed_vote_trend_cache_ttl(parameters), rows)
    return rows


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def _atr(*, highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float]:
    true_ranges: list[float] = []
    previous_close: float | None = None
    for high, low, close in zip(highs, lows, closes, strict=True):
        true_range = high - low if previous_close is None else max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )
        true_ranges.append(true_range)
        previous_close = close
    return _ema(true_ranges, period)


async def _fixed_vol_breakout_signals(runtime: TraderRuntime, symbol: str) -> list[CandidateSignal] | None:
    binding = runtime.startup_snapshot.strategy_binding_for(symbol, StrategyId.VOL_BREAKOUT.value)
    parameters = _parse_fixed_vol_breakout_parameters(binding)
    if parameters is None:
        return None
    rows = await _get_fixed_vol_breakout_rows(
        runtime,
        symbol=symbol,
        parameters=parameters,
        failure_detail_prefix="fixed_vol_breakout_history_fetch_failed",
    )
    if rows is None:
        return []
    warmup = max(parameters.ema_period, parameters.atr_period + 1)
    if len(rows) <= warmup:
        return []
    closes = [float(row["close"]) for row in rows]
    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]
    ema_values = _ema(closes, parameters.ema_period)
    atr_values = _atr(highs=highs, lows=lows, closes=closes, period=parameters.atr_period)
    index = len(rows) - 1
    candle_key = str(rows[index]["timestamp"])
    runtime.vol_breakout_signal_candles[symbol] = candle_key
    if runtime.vol_breakout_entry_candles.get(symbol) == candle_key:
        return []
    close = closes[index]
    breakout_level = closes[index - 1] + parameters.k * atr_values[index - 1]
    if close <= ema_values[index] or close <= breakout_level:
        return []
    return [
        CandidateSignal(
            symbol=symbol,
            strategy_id=StrategyId.VOL_BREAKOUT,
            side=OrderSide.BUY,
            entry_type=EntryType.MARKET,
            urgency=SignalUrgency.HIGH,
            confidence=0.7,
            max_hold_ms=3000,
            cancel_after_ms=750,
            risk_tag="vol_breakout",
        )
    ]


async def _fixed_vote_trend_signals(runtime: TraderRuntime, symbol: str) -> list[CandidateSignal] | None:
    binding = runtime.startup_snapshot.strategy_binding_for(symbol, StrategyId.VOTE_TREND.value)
    parameters = _parse_fixed_vote_trend_parameters(binding)
    if parameters is None:
        return None
    rows = await _get_fixed_vote_trend_rows(
        runtime,
        symbol=symbol,
        parameters=parameters,
        failure_detail_prefix="fixed_vote_trend_history_fetch_failed",
    )
    if rows is None:
        return []
    signal_side = latest_vote_trend_side(parameters.to_vote_trend_parameters(), rows)
    if signal_side is None:
        return []
    index = len(rows) - 1
    candle_key = str(rows[index]["timestamp"])
    runtime.vote_trend_signal_candles[symbol] = candle_key
    if runtime.vote_trend_entry_candles.get(symbol) == candle_key:
        return []
    return [
        CandidateSignal(
            symbol=symbol,
            strategy_id=StrategyId.VOTE_TREND,
            side=OrderSide.BUY if signal_side == "long" else OrderSide.SELL,
            entry_type=EntryType.MARKET,
            urgency=SignalUrgency.HIGH,
            confidence=0.75,
            max_hold_ms=3000,
            cancel_after_ms=750,
            risk_tag="vote_trend",
        )
    ]


async def _submit_signal_open(runtime: TraderRuntime, signal: CandidateSignal) -> bool:
    decision = runtime.components.risk_kernel.evaluate(signal, runtime.startup_snapshot)
    if _is_open_execution_failure_cooling_down(runtime, signal):
        runtime.long_entry_confirmations.pop(signal.symbol, None)
        return False
    client_order_id = _next_client_order_id(runtime, signal)
    requested_order_size = max(1.0, decision.max_order_size)
    order_size, margin_block_reason = _margin_adjusted_open_order_size(
        runtime,
        signal.symbol,
        requested_order_size,
    )
    if margin_block_reason is not None:
        runtime.history_store.append_risk_event(
            {
                "event_type": "signal_blocked",
                "symbol": signal.symbol,
                "detail": margin_block_reason,
                "strategy_id": signal.strategy_id.value,
                "requested_size": requested_order_size,
                "available_balance": runtime.components.state_engine.account_state.available_balance,
            }
        )
        return False
    strategy_logic = _build_strategy_logic(signal, runtime.startup_snapshot)
    runtime.components.state_engine.stage_order_submission(
        signal.symbol,
        client_order_id=client_order_id,
        side=signal.side.value,
        size=order_size,
        intent="open",
        strategy_id=signal.strategy_id.value,
        strategy_logic=strategy_logic,
    )
    runtime.pending_position_actions[signal.symbol] = "open"
    try:
        response = await runtime.execution_coordinator.submit_market_open(
            symbol=signal.symbol,
            side=signal.side.value,
            size=order_size,
            client_order_id=client_order_id,
            decision=decision,
            timestamp=_now_timestamp(),
        )
    except Exception as exc:
        runtime.pending_position_actions.pop(signal.symbol, None)
        runtime.components.state_engine.clear_order_submission(signal.symbol, client_order_id)
        cooldown_until = _start_open_execution_failure_cooldown(runtime, signal)
        runtime.history_store.append_risk_event(
            {
                "event_type": "execution_submission_failed",
                "symbol": signal.symbol,
                "detail": _format_execution_error(exc),
                "cooldown_until": cooldown_until,
                "strategy_id": signal.strategy_id.value,
            }
        )
        return False
    if response is None:
        runtime.pending_position_actions.pop(signal.symbol, None)
        runtime.components.state_engine.clear_order_submission(signal.symbol, client_order_id)
        runtime.history_store.append_risk_event(
            {
                "event_type": "signal_blocked",
                "symbol": signal.symbol,
                "detail": ",".join(decision.reason_codes),
            }
        )
        return False
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
                "strategy_logic": strategy_logic,
            }
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
    runtime.long_entry_confirmations.pop(signal.symbol, None)
    if signal.strategy_id == StrategyId.VOL_BREAKOUT:
        candle_key = runtime.vol_breakout_signal_candles.get(signal.symbol)
        if candle_key is not None:
            runtime.vol_breakout_entry_candles[signal.symbol] = candle_key
    if signal.strategy_id == StrategyId.VOTE_TREND:
        candle_key = runtime.vote_trend_signal_candles.get(signal.symbol)
        if candle_key is not None:
            runtime.vote_trend_entry_candles[signal.symbol] = candle_key
    return True


def _margin_adjusted_open_order_size(
    runtime: TraderRuntime,
    symbol: str,
    requested_size: float,
) -> tuple[float, str | None]:
    account_state = runtime.components.state_engine.account_state
    market_snapshot = runtime.components.state_engine.snapshot(symbol)
    result = calculate_open_order_size(
        OpenOrderSizingInput(
            symbol=symbol,
            requested_size=requested_size,
            mark_price=market_snapshot.mid_price,
            equity=account_state.equity,
            available_balance=account_state.available_balance,
            starting_nav=runtime.starting_nav,
            max_leverage=runtime.startup_snapshot.max_leverage,
        )
    )
    return result.order_size, result.block_reason


async def _submit_position_close(
    runtime: TraderRuntime,
    *,
    symbol: str,
    position: object,
    strategy_id: StrategyId,
    strategy_logic: str,
) -> bool:
    position_side = str(getattr(position, "position_side", "long") or "long")
    position_quantity = abs(float(getattr(position, "net_quantity", 0.0) or 0.0))
    if position_quantity <= 0.0:
        return False
    signal = CandidateSignal(
        symbol=symbol,
        strategy_id=strategy_id,
        side=_closing_order_side(position_side),
        entry_type=EntryType.MARKET,
        urgency=SignalUrgency.IMMEDIATE,
        confidence=1.0,
        max_hold_ms=1,
        cancel_after_ms=1,
        risk_tag=f"close_{position_side}",
    )
    decision = runtime.components.risk_kernel.evaluate(signal, runtime.startup_snapshot)
    client_order_id = _next_client_order_id(runtime, signal)
    runtime.components.state_engine.stage_order_submission(
        symbol,
        client_order_id=client_order_id,
        side=signal.side.value,
        size=position_quantity,
        intent="close",
        strategy_id=strategy_id.value,
        strategy_logic=strategy_logic,
    )
    runtime.pending_position_actions[symbol] = "close"
    try:
        response = await runtime.execution_coordinator.submit_market_close(
            symbol=symbol,
            side=signal.side.value,
            size=position_quantity,
            client_order_id=client_order_id,
            decision=decision,
            timestamp=_now_timestamp(),
            position_side=position_side,
        )
    except Exception as exc:
        runtime.pending_position_actions.pop(symbol, None)
        runtime.components.state_engine.clear_order_submission(symbol, client_order_id)
        runtime.history_store.append_risk_event(
            {
                "event_type": "execution_submission_failed",
                "symbol": symbol,
                "detail": _format_execution_error(exc),
            }
        )
        return False
    if response is None:
        runtime.pending_position_actions.pop(symbol, None)
        runtime.components.state_engine.clear_order_submission(symbol, client_order_id)
        runtime.history_store.append_risk_event(
            {
                "event_type": "signal_blocked",
                "symbol": symbol,
                "detail": ",".join(decision.reason_codes),
            }
        )
        return False
    for row in response:
        runtime.history_store.append_order_fact(
            {
                "symbol": symbol,
                "side": signal.side.value,
                "status": "submitted",
                "client_order_id": str(row.get("clOrdId") or ""),
                "order_id": str(row.get("ordId") or ""),
                "intent": "close",
                "strategy_id": strategy_id.value,
                "strategy_logic": strategy_logic,
            }
        )
    _LOGGER.info(
        "order_submitted",
        extra={
            "service": "trader",
            "symbol": symbol,
            "strategy_id": strategy_id.value,
            "side": signal.side.value,
            "client_order_id": response[0].get("clOrdId") or "",
            "run_mode": runtime.current_mode.value,
        },
    )
    return True


def _build_strategy_logic(signal: CandidateSignal, snapshot: StrategyConfigSnapshot) -> str:
    if signal.strategy_id == StrategyId.VOL_BREAKOUT:
        binding = snapshot.strategy_binding_for(signal.symbol, signal.strategy_id.value)
        bar = _extract_vol_breakout_bar(getattr(binding, "strategy_def_id", ""))
        return f"{signal.symbol} {bar} 波动率突破，价格突破 ATR 阈值后顺势开多。"
    if signal.strategy_id == StrategyId.VOTE_TREND:
        binding = snapshot.strategy_binding_for(signal.symbol, signal.strategy_id.value)
        parameters = _parse_fixed_vote_trend_parameters(binding)
        bar = parameters.bar if parameters is not None else "12H"
        direction = "开多" if signal.side == OrderSide.BUY else "开空"
        return f"{signal.symbol} {bar} 多因子趋势投票，EMA/动量/通道/RSI 达到阈值后顺势{direction}。"
    if signal.strategy_id == StrategyId.SHORT_MOMENTUM:
        return f"{signal.symbol} 4H 空头动量破位，价格跌破回看阈值后顺势开空。"
    if signal.strategy_id == StrategyId.BREAKOUT:
        return "趋势突破，最近成交偏买方，准备顺势开多。"
    if signal.strategy_id == StrategyId.MEAN_REVERSION:
        return "均值回归，价格偏离后尝试反向回补。"
    return "风险暂停信号，当前不执行新开仓。"


def _build_position_strategy_logic(symbol: str, position_side: str, snapshot: StrategyConfigSnapshot) -> tuple[str, str]:
    strategy_id = _strategy_id_for_position_side(position_side, snapshot=snapshot, symbol=symbol)
    signal_side = OrderSide.BUY if position_side == "long" else OrderSide.SELL
    signal = CandidateSignal(
        symbol=symbol,
        strategy_id=strategy_id,
        side=signal_side,
        entry_type=EntryType.MARKET,
        urgency=SignalUrgency.NORMAL,
        confidence=1.0,
        max_hold_ms=1,
        cancel_after_ms=1,
        risk_tag=f"{strategy_id.value}_position",
    )
    return strategy_id.value, _build_strategy_logic(signal, snapshot)


def _position_context_from_runtime(runtime: TraderRuntime, event: PositionUpdateEvent) -> dict[str, str]:
    context = runtime.position_entry_contexts.get(event.symbol)
    if context is not None and context.position_side == event.position_side:
        return {
            "intent": "open",
            "strategy_id": context.strategy_id,
            "strategy_logic": context.strategy_logic,
        }
    return {}


def _state_trade_context_for_position(
    runtime: TraderRuntime,
    symbol: str,
    position_side: str,
) -> dict[str, str]:
    trade_context = runtime.components.state_engine.trade_context_by_symbol.get(symbol, {})
    strategy_id = trade_context.get("strategy_id")
    strategy_logic = trade_context.get("strategy_logic")
    expected_strategy_id = _strategy_id_for_position_side(
        position_side,
        snapshot=runtime.startup_snapshot,
        symbol=symbol,
    ).value
    if (
        strategy_id != expected_strategy_id
        or strategy_logic is None
        or not runtime.startup_snapshot.is_strategy_enabled(expected_strategy_id)
    ):
        return {}
    context = {}
    intent = trade_context.get("intent")
    if intent is not None:
        context["intent"] = intent
    context["strategy_id"] = strategy_id
    context["strategy_logic"] = strategy_logic
    return context


def _sync_position_entry_context(runtime: TraderRuntime, event: PositionUpdateEvent) -> None:
    pending_action = runtime.pending_position_actions.get(event.symbol)
    if pending_action == "open" and event.net_quantity != 0.0:
        runtime.pending_position_actions.pop(event.symbol, None)
    elif pending_action == "close" and event.net_quantity == 0.0:
        runtime.pending_position_actions.pop(event.symbol, None)
        runtime.long_entry_confirmations.pop(event.symbol, None)
    if event.net_quantity == 0.0:
        runtime.position_entry_contexts.pop(event.symbol, None)
        return
    mark_price = event.mark_price if event.mark_price > 0.0 else event.average_price
    existing = runtime.position_entry_contexts.get(event.symbol)
    if existing is not None and existing.position_side == event.position_side:
        existing.quantity = event.net_quantity
        existing.entry_price = event.average_price
        existing.highest_mark_price = max(existing.highest_mark_price, mark_price)
        lowest = existing.lowest_mark_price if existing.lowest_mark_price > 0.0 else mark_price
        existing.lowest_mark_price = min(lowest, mark_price)
        return
    trade_context = _state_trade_context_for_position(runtime, event.symbol, event.position_side)
    strategy_id = trade_context.get("strategy_id")
    strategy_logic = trade_context.get("strategy_logic")
    if strategy_id is None or strategy_logic is None:
        inferred_strategy_id, inferred_logic = _build_position_strategy_logic(
            event.symbol,
            event.position_side,
            runtime.startup_snapshot,
        )
        strategy_id = strategy_id or inferred_strategy_id
        strategy_logic = strategy_logic or inferred_logic
    runtime.position_entry_contexts[event.symbol] = PositionEntryContext(
        symbol=event.symbol,
        position_side=event.position_side,
        quantity=event.net_quantity,
        entry_price=event.average_price,
        entered_at=event.generated_at,
        strategy_id=strategy_id,
        strategy_logic=strategy_logic,
        highest_mark_price=mark_price,
        lowest_mark_price=mark_price,
    )


def _extract_vol_breakout_bar(strategy_def_id: object) -> str:
    parts = str(strategy_def_id or "").split("-")
    for part in parts:
        normalized = part.upper()
        if normalized.endswith(("M", "H", "D")) and normalized[:-1].isdigit():
            return normalized
    return "固定周期"


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


def _apply_symbol_strategy_bindings(runtime: TraderRuntime, snapshot: StrategyConfigSnapshot) -> None:
    for binding_key, candidate_strategy in _snapshot_strategy_bindings(snapshot).items():
        symbol = binding_key.split(":", 1)[0]
        current_strategy = runtime.active_symbol_strategies.get(binding_key)
        if (
            current_strategy is not None
            and current_strategy.strategy_def_id == candidate_strategy.strategy_def_id
            and current_strategy.strategy_package_id == candidate_strategy.strategy_package_id
        ):
            runtime.active_symbol_strategies[binding_key] = candidate_strategy
            continue
        if not _is_stronger_replacement(current_strategy, candidate_strategy):
            continue

        events = _build_strategy_handover_events(symbol, current_strategy, candidate_strategy)
        runtime.symbol_handover_state[binding_key] = {
            "status": "handover_pending",
            "events": events,
            "next_strategy_def_id": candidate_strategy.strategy_def_id,
            "next_strategy_package_id": candidate_strategy.strategy_package_id,
        }
        if current_strategy is not None:
            runtime.history_store.append_strategy_replacement(
                {
                    "symbol": symbol,
                    "current_strategy_def_id": current_strategy.strategy_def_id,
                    "current_strategy_package_id": current_strategy.strategy_package_id,
                    "next_strategy_def_id": candidate_strategy.strategy_def_id,
                    "next_strategy_package_id": candidate_strategy.strategy_package_id,
                    "current_score": current_strategy.score,
                    "next_score": candidate_strategy.score,
                    "score_basis": candidate_strategy.score_basis,
                }
            )
        runtime.active_symbol_strategies[binding_key] = candidate_strategy


def _can_relax_to_snapshot_mode(runtime: TraderRuntime, snapshot: StrategyConfigSnapshot) -> bool:
    return (
        _RUN_MODE_PRIORITY[runtime.current_mode] > _RUN_MODE_PRIORITY[snapshot.market_mode]
        and snapshot.approval_state == ApprovalState.APPROVED
        and snapshot.market_mode != RunMode.HALTED
        and runtime.components.checkpoint_service.can_open_new_risk(runtime.startup_checkpoint)
        and not runtime.components.state_engine.fault_flags
    )


def _can_apply_manual_release(runtime: TraderRuntime, target_mode: RunMode) -> bool:
    return (
        _RUN_MODE_PRIORITY[target_mode] < _RUN_MODE_PRIORITY[runtime.current_mode]
        and target_mode != RunMode.HALTED
        and runtime.startup_snapshot.approval_state == ApprovalState.APPROVED
        and runtime.components.checkpoint_service.can_open_new_risk(runtime.startup_checkpoint)
        and not runtime.components.state_engine.fault_flags
    )


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _account_synced_strategy_total(runtime: TraderRuntime) -> float | None:
    account_state = runtime.components.state_engine.account_state
    if account_state.equity > 0.0:
        return account_state.equity
    if account_state.available_balance > 0.0:
        return account_state.available_balance
    return None


def _sync_strategy_capital(runtime: TraderRuntime) -> None:
    account_strategy_total = _account_synced_strategy_total(runtime)
    if account_strategy_total is not None:
        runtime.starting_nav = account_strategy_total
        runtime.components.risk_kernel.nav = account_strategy_total
        return
    summary = runtime.runtime_store.get_budget_pool_summary()
    if not isinstance(summary, dict):
        return
    if summary.get("manual_strategy_total_amount_override") is not True:
        return
    strategy_total_amount = _positive_float(summary.get("strategy_total_amount"))
    if strategy_total_amount is None:
        return
    runtime.starting_nav = strategy_total_amount
    runtime.components.risk_kernel.nav = strategy_total_amount


def _sync_manual_runtime_controls(runtime: TraderRuntime) -> None:
    _sync_strategy_capital(runtime)
    requested_mode = runtime.runtime_store.get_run_mode()
    if requested_mode is not None and _RUN_MODE_PRIORITY[requested_mode] > _RUN_MODE_PRIORITY[runtime.current_mode]:
        _tighten_runtime_mode(runtime, requested_mode)
        _publish_runtime_state(runtime)

    release_target = runtime.runtime_store.get_manual_release_target()
    if not release_target:
        return
    try:
        target_mode = RunMode(release_target)
    except ValueError:
        runtime.runtime_store.clear_manual_release_target()
        return
    if not _can_apply_manual_release(runtime, target_mode):
        return
    previous_mode = runtime.current_mode
    runtime.current_mode = target_mode
    runtime.components.state_engine.set_run_mode(target_mode)
    runtime.startup_checkpoint.current_mode = target_mode
    runtime.opening_allowed = target_mode not in {RunMode.REDUCE_ONLY, RunMode.HALTED}
    runtime.runtime_store.clear_manual_release_target()
    _publish_runtime_state(runtime)
    runtime.history_store.append_risk_event(
        {
            "event_type": "manual_release_applied",
            "symbol": "system",
            "detail": f"released {previous_mode.value} to {target_mode.value}",
        }
    )


async def _evaluate_symbol(runtime: TraderRuntime, symbol: str) -> None:
    _sync_manual_runtime_controls(runtime)
    if runtime.current_mode == RunMode.HALTED:
        return
    if not _uses_fixed_strategy_snapshot(runtime):
        latest_snapshot = runtime.snapshot_store.get_latest_snapshot()
        if latest_snapshot is not None:
            runtime.startup_snapshot = latest_snapshot
            _apply_symbol_strategy_bindings(runtime, latest_snapshot)
            if _can_relax_to_snapshot_mode(runtime, latest_snapshot):
                runtime.current_mode = latest_snapshot.market_mode
                runtime.components.state_engine.set_run_mode(runtime.current_mode)
                runtime.opening_allowed = True
                _publish_runtime_state(runtime)
    if runtime.startup_snapshot.version_id == "bootstrap":
        return
    snapshot = runtime.components.state_engine.snapshot(symbol)
    fixed_vote_signals = await _fixed_vote_trend_signals(runtime, symbol)
    fixed_vol_signals = None if fixed_vote_signals is not None else await _fixed_vol_breakout_signals(runtime, symbol)
    if fixed_vote_signals is None and fixed_vol_signals is None:
        signals = _enabled_candidate_signals(build_candidate_signals(snapshot), runtime.startup_snapshot)
    else:
        signals = fixed_vote_signals if fixed_vote_signals is not None else fixed_vol_signals
    open_orders = runtime.components.state_engine.open_orders_by_symbol.get(symbol, {})
    position = runtime.components.state_engine.positions_by_symbol.get(symbol)
    position_quantity = position.net_quantity if position is not None else 0.0
    position_side = position.position_side if position is not None else "long"
    has_open_orders = bool(open_orders)
    if symbol in runtime.pending_position_actions:
        return
    has_exposure = has_open_orders or position_quantity != 0.0
    pending_reverse_signal = runtime.pending_reverse_signals.get(symbol)
    if pending_reverse_signal is not None:
        if has_exposure:
            return
        if not runtime.startup_snapshot.is_strategy_enabled(pending_reverse_signal.strategy_id.value):
            runtime.pending_reverse_signals.pop(symbol, None)
            runtime.history_store.append_risk_event(
                {
                    "event_type": "signal_blocked",
                    "symbol": symbol,
                    "detail": "pending_reverse_strategy_disabled",
                    "strategy_id": pending_reverse_signal.strategy_id.value,
                }
            )
            return
        if await _submit_signal_open(runtime, pending_reverse_signal):
            runtime.pending_reverse_signals.pop(symbol, None)
        return
    if position is not None and position_quantity != 0.0 and not has_open_orders:
        exit_reason = await _position_exit_reason(runtime, symbol, position)
        if exit_reason is not None:
            await _submit_position_close(
                runtime,
                symbol=symbol,
                position=position,
                strategy_id=_strategy_id_for_position_side(
                    position_side,
                    snapshot=runtime.startup_snapshot,
                    symbol=symbol,
                ),
                strategy_logic=f"{symbol} {position_side} 持仓触发退出：{exit_reason}。",
            )
            return
        reverse_signal = _opposite_signal(position_side, signals, runtime.startup_snapshot)
        if reverse_signal is not None:
            if await _submit_position_close(
                runtime,
                symbol=symbol,
                position=position,
                strategy_id=reverse_signal.strategy_id,
                strategy_logic=f"{symbol} 反向信号触发，先平 {position_side} 持仓，等待仓位归零后再开反向仓。",
            ):
                runtime.pending_reverse_signals[symbol] = reverse_signal
            return
    for signal in _prioritize_strategy_signals(signals):
        decision = runtime.components.risk_kernel.evaluate(signal, runtime.startup_snapshot)
        if signal.entry_type != EntryType.MARKET or signal.side not in {OrderSide.BUY, OrderSide.SELL}:
            runtime.long_entry_confirmations.pop(symbol, None)
            continue
        if has_open_orders:
            continue
        if _is_short_priority_long_close(signal, position):
            if not runtime.startup_snapshot.is_strategy_enabled(signal.strategy_id.value):
                runtime.history_store.append_risk_event(
                    {
                        "event_type": "signal_blocked",
                        "symbol": signal.symbol,
                        "detail": "strategy_disabled",
                    }
                )
                continue
            client_order_id = _next_client_order_id(runtime, signal)
            strategy_logic = "空头优先信号触发，先平多头，等待仓位归零后再开空。"
            runtime.components.state_engine.stage_order_submission(
                signal.symbol,
                client_order_id=client_order_id,
                side=OrderSide.SELL.value,
                size=abs(position_quantity),
                intent="close",
                strategy_id=signal.strategy_id.value,
                strategy_logic=strategy_logic,
            )
            try:
                response = await runtime.execution_coordinator.submit_market_close(
                    symbol=signal.symbol,
                    side=OrderSide.SELL.value,
                    size=abs(position_quantity),
                    client_order_id=client_order_id,
                    decision=decision,
                    timestamp=_now_timestamp(),
                    position_side="long",
                )
            except Exception as exc:
                runtime.components.state_engine.clear_order_submission(signal.symbol, client_order_id)
                runtime.history_store.append_risk_event(
                    {
                        "event_type": "execution_submission_failed",
                        "symbol": signal.symbol,
                        "detail": _format_execution_error(exc),
                    }
                )
                continue
            if response is None:
                runtime.components.state_engine.clear_order_submission(signal.symbol, client_order_id)
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
                        "side": OrderSide.SELL.value,
                        "status": "submitted",
                        "client_order_id": str(row.get("clOrdId") or ""),
                        "order_id": str(row.get("ordId") or ""),
                        "intent": "close",
                        "strategy_id": signal.strategy_id.value,
                        "strategy_logic": strategy_logic,
                    }
                )
            _LOGGER.info(
                "order_submitted",
                extra={
                    "service": "trader",
                    "symbol": signal.symbol,
                    "strategy_id": signal.strategy_id.value,
                    "side": OrderSide.SELL.value,
                    "client_order_id": response[0].get("clOrdId") or "",
                    "run_mode": runtime.current_mode.value,
                },
            )
            runtime.pending_reverse_signals[signal.symbol] = signal
            has_exposure = True
            continue
        if has_exposure:
            runtime.long_entry_confirmations.pop(signal.symbol, None)
            continue
        if _is_open_execution_failure_cooling_down(runtime, signal):
            runtime.long_entry_confirmations.pop(signal.symbol, None)
            continue
        if not _long_entry_confirmed(runtime, signal):
            runtime.history_store.append_risk_event(
                {
                    "event_type": "signal_blocked",
                    "symbol": signal.symbol,
                    "detail": "waiting_long_entry_confirmation",
                }
            )
            continue
        if await _submit_signal_open(runtime, signal):
            has_exposure = True


def _stream_failure_severity(adapter: object) -> str:
    if isinstance(adapter, OkxPublicStream):
        return "warn"
    return "critical"


def _is_retryable_public_stream_disconnect(adapter: object, exc: Exception) -> bool:
    if not isinstance(adapter, OkxPublicStream):
        return False
    if isinstance(exc, TimeoutError):
        return True
    detail = str(exc).lower()
    return any(marker in detail for marker in _PUBLIC_STREAM_RECONNECT_MARKERS)


async def _consume_stream(
    runtime: TraderRuntime,
    adapter: object,
    *,
    reconnect_delay_seconds: float = 5.0,
    max_reconnect_attempts: int | None = None,
    **kwargs: object,
) -> bool:
    iterator_factory = getattr(adapter, "iter_events", None)
    if not callable(iterator_factory):
        return False
    consumed_any = False
    reconnect_attempts = 0
    while True:
        try:
            async for event in iterator_factory(**kwargs):
                consumed_any = True
                reconnect_attempts = 0
                await _dispatch_runtime_event(runtime, event)
            return consumed_any
        except Exception as exc:
            reconnect_attempts += 1
            if _is_retryable_public_stream_disconnect(adapter, exc):
                if max_reconnect_attempts is not None and reconnect_attempts >= max_reconnect_attempts:
                    return consumed_any
                _LOGGER.warning(
                    "public_stream_reconnect_scheduled",
                    extra={
                        "service": "trader",
                        "adapter": adapter.__class__.__name__,
                        "attempt": reconnect_attempts,
                        "delay_seconds": reconnect_delay_seconds,
                        "detail": str(exc),
                    },
                )
                await asyncio.sleep(reconnect_delay_seconds)
                continue
            await _dispatch_runtime_event(
                runtime,
                FaultEvent(
                    event_type=TraderEventType.RUNTIME_FAULT,
                    exchange="okx",
                    generated_at=datetime.now(UTC),
                    severity=_stream_failure_severity(adapter),
                    code=f"{adapter.__class__.__name__.lower()}_stream_failed",
                    detail=str(exc),
                ),
            )
            if max_reconnect_attempts is not None and reconnect_attempts >= max_reconnect_attempts:
                return consumed_any
            _LOGGER.warning(
                "stream_reconnect_scheduled",
                extra={
                    "service": "trader",
                    "adapter": adapter.__class__.__name__,
                    "attempt": reconnect_attempts,
                    "delay_seconds": reconnect_delay_seconds,
                },
            )
            await asyncio.sleep(reconnect_delay_seconds)


async def _dispatch_runtime_event(runtime: TraderRuntime, event: object) -> None:
    _sync_manual_runtime_controls(runtime)
    dispatch_event(runtime.components.state_engine, event)
    _try_recover_runtime_after_stream_event(runtime, event)
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
        _sync_position_entry_context(runtime, event)
        trade_context = _position_context_from_runtime(runtime, event)
        runtime.history_store.append_position_fact(
            {
                "symbol": event.symbol,
                "net_quantity": event.net_quantity,
                "position_side": event.position_side,
                "average_price": event.average_price,
                "mark_price": event.mark_price,
                "unrealized_pnl": event.unrealized_pnl,
                **trade_context,
            }
        )
        if event.net_quantity == 0.0 and event.symbol in runtime.pending_reverse_signals:
            await _evaluate_symbol(runtime, event.symbol)
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
    if not _uses_fixed_strategy_snapshot(runtime):
        latest_snapshot = runtime.snapshot_store.get_latest_snapshot()
        if latest_snapshot is not None:
            runtime.startup_snapshot = latest_snapshot
            _apply_symbol_strategy_bindings(runtime, latest_snapshot)
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

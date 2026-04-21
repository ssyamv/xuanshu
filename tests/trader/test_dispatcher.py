from datetime import UTC, datetime

from xuanshu.contracts.events import (
    AccountSnapshotEvent,
    FaultEvent,
    MarketTradeEvent,
    OrderUpdateEvent,
    OrderbookTopEvent,
    PositionUpdateEvent,
)
from xuanshu.core.enums import RunMode, TraderEventType
from xuanshu.state.engine import StateEngine
from xuanshu.trader.dispatcher import dispatch_event


def test_state_engine_tracks_market_orders_positions_mode_and_faults() -> None:
    engine = StateEngine()

    engine.on_orderbook_top(
        OrderbookTopEvent(
            event_type=TraderEventType.ORDERBOOK_TOP,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            public_sequence="pub-1",
            bid_price=100.0,
            ask_price=100.1,
            bid_size=5.0,
            ask_size=6.0,
        )
    )
    engine.on_order_update(
        OrderUpdateEvent(
            event_type=TraderEventType.ORDER_UPDATE,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            private_sequence="pri-1",
            order_id="ord-1",
            client_order_id="btc-breakout-000001",
            side="buy",
            price=100.1,
            size=1.0,
            filled_size=0.0,
            status="live",
        )
    )
    engine.on_position_update(
        PositionUpdateEvent(
            event_type=TraderEventType.POSITION_UPDATE,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            private_sequence="pri-2",
            net_quantity=1.0,
            average_price=100.1,
            mark_price=100.2,
            unrealized_pnl=0.1,
        )
    )
    engine.on_fault(
        FaultEvent(
            event_type=TraderEventType.RUNTIME_FAULT,
            exchange="okx",
            generated_at=datetime.now(UTC),
            severity="warn",
            code="public_ws_disconnected",
            detail="public stream dropped",
        )
    )
    engine.set_run_mode(RunMode.REDUCE_ONLY)

    summary = engine.build_symbol_runtime_summary("BTC-USDT-SWAP")

    assert summary["symbol"] == "BTC-USDT-SWAP"
    assert summary["open_order_count"] == 1
    assert summary["net_quantity"] == 1.0
    assert summary["run_mode"] == "reduce_only"
    assert "public_ws_disconnected" in engine.fault_flags


def test_state_engine_propagates_stream_markers_and_runtime_snapshot_state() -> None:
    engine = StateEngine()
    engine.set_run_mode(RunMode.REDUCE_ONLY)

    engine.on_orderbook_top(
        OrderbookTopEvent(
            event_type=TraderEventType.ORDERBOOK_TOP,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            public_sequence="pub-9",
            bid_price=100.0,
            ask_price=100.2,
            bid_size=4.0,
            ask_size=5.0,
        )
    )
    engine.on_position_update(
        PositionUpdateEvent(
            event_type=TraderEventType.POSITION_UPDATE,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            private_sequence="pri-9",
            net_quantity=2.5,
            average_price=100.1,
            mark_price=100.3,
            unrealized_pnl=0.5,
        )
    )

    summary = engine.build_symbol_runtime_summary("BTC-USDT-SWAP")
    snapshot = engine.snapshot("BTC-USDT-SWAP")

    assert engine.last_public_stream_marker == "pub-9"
    assert engine.last_private_stream_marker == "pri-9"
    assert summary["run_mode"] == "reduce_only"
    assert summary["net_quantity"] == 2.5
    assert summary["mid_price"] == snapshot.mid_price
    assert summary["spread"] == snapshot.spread
    assert summary["regime"] == snapshot.regime.value
    assert snapshot.current_mode == RunMode.REDUCE_ONLY
    assert snapshot.current_position == 2.5


def test_state_engine_removes_terminal_orders_and_keeps_latest_private_marker() -> None:
    engine = StateEngine()

    engine.on_order_update(
        OrderUpdateEvent(
            event_type=TraderEventType.ORDER_UPDATE,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            private_sequence="pri-live",
            order_id="ord-1",
            client_order_id="btc-breakout-000001",
            side="buy",
            price=100.1,
            size=1.0,
            filled_size=0.0,
            status="live",
        )
    )
    engine.on_order_update(
        OrderUpdateEvent(
            event_type=TraderEventType.ORDER_UPDATE,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            private_sequence="pri-cancel",
            order_id="ord-1",
            client_order_id="btc-breakout-000001",
            side="buy",
            price=100.1,
            size=1.0,
            filled_size=0.0,
            status=" Cancelled ",
        )
    )

    assert engine.last_private_stream_marker == "pri-cancel"
    assert engine.open_orders_by_symbol["BTC-USDT-SWAP"] == {}


def test_state_engine_clears_staged_close_orders_when_position_is_flat() -> None:
    engine = StateEngine()
    engine.stage_order_submission(
        "BTC-USDT-SWAP",
        client_order_id="BTCUSDTSWAPshortmomentum000001",
        side="sell",
        size=3.5,
        intent="close",
        strategy_id="short_momentum",
        strategy_logic="空头优先信号触发，先平多头，等待仓位归零后再开空。",
    )

    engine.on_position_update(
        PositionUpdateEvent(
            event_type=TraderEventType.POSITION_UPDATE,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            private_sequence="pri-flat",
            net_quantity=0.0,
            average_price=0.0,
            mark_price=0.0,
            unrealized_pnl=0.0,
        )
    )

    assert engine.open_orders_by_symbol["BTC-USDT-SWAP"] == {}


def test_dispatcher_routes_market_trade_and_account_snapshot_into_state() -> None:
    engine = StateEngine()

    dispatch_event(
        engine,
        OrderbookTopEvent(
            event_type=TraderEventType.ORDERBOOK_TOP,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            public_sequence="pub-1",
            bid_price=100.0,
            ask_price=100.2,
            bid_size=5.0,
            ask_size=6.0,
        ),
    )
    dispatch_event(
        engine,
        MarketTradeEvent(
            event_type=TraderEventType.MARKET_TRADE,
            symbol="BTC-USDT-SWAP",
            exchange="okx",
            generated_at=datetime.now(UTC),
            public_sequence="pub-2",
            price=100.3,
            size=2.0,
            side="buy",
        ),
    )
    dispatch_event(
        engine,
        AccountSnapshotEvent(
            event_type=TraderEventType.ACCOUNT_SNAPSHOT,
            exchange="okx",
            generated_at=datetime.now(UTC),
            private_sequence="pri-1",
            equity=10_000.0,
            available_balance=7_500.0,
            margin_ratio=0.25,
        ),
    )

    snapshot = engine.snapshot("BTC-USDT-SWAP")
    budget_summary = engine.build_budget_pool_summary()

    assert engine.last_public_stream_marker == "pub-2"
    assert engine.last_private_stream_marker == "pri-1"
    assert snapshot.recent_trade_bias == 1.0
    assert budget_summary["equity"] == 10_000.0
    assert budget_summary["available_balance"] == 7_500.0
    assert budget_summary["margin_ratio"] == 0.25

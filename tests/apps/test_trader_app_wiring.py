import xuanshu.apps.trader as trader_app


def test_trader_entrypoint_builds_typed_components(monkeypatch) -> None:
    original_build_trader_components = trader_app.build_trader_components
    build_called = 0

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)

    def fake_build_trader_components() -> trader_app.TraderComponents:
        nonlocal build_called
        build_called += 1
        return original_build_trader_components()

    monkeypatch.setattr(trader_app, "build_trader_components", fake_build_trader_components)

    assert trader_app.main() == 0

    components = original_build_trader_components()
    assert components.state_engine.__class__.__name__ == "StateEngine"
    assert components.risk_kernel.nav == 100_000.0
    assert components.checkpoint_service.__class__.__name__ == "CheckpointService"
    assert components.client_order_id_builder("BTC-USDT-SWAP", "breakout", 1) == "BTC-USDT-SWAP-breakout-000001"
    assert build_called == 1

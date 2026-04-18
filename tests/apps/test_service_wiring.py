import xuanshu.apps.governor as governor_app
import xuanshu.apps.notifier as notifier_app
import xuanshu.apps.trader as trader_app
from xuanshu.core.enums import RunMode
from xuanshu.apps.governor import build_governor_service
from xuanshu.apps.notifier import build_notifier_preview


def test_service_entrypoints_build_and_startup_paths_are_wired(monkeypatch) -> None:
    original_build_trader_components = trader_app.build_trader_components
    original_build_governor_service = build_governor_service
    original_build_notifier_preview = build_notifier_preview

    trader_called = 0
    governor_called = 0
    notifier_called = 0

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(trader_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)
    monkeypatch.setattr(notifier_app, "_wait_forever", _noop_wait_forever)

    def fake_build_trader_components() -> dict[str, object]:
        nonlocal trader_called
        trader_called += 1
        return original_build_trader_components()

    def fake_build_governor_service():
        nonlocal governor_called
        governor_called += 1
        return original_build_governor_service()

    def fake_build_notifier_preview(mode: RunMode) -> str:
        nonlocal notifier_called
        notifier_called += 1
        return original_build_notifier_preview(mode)

    monkeypatch.setattr(trader_app, "build_trader_components", fake_build_trader_components)
    monkeypatch.setattr(governor_app, "build_governor_service", fake_build_governor_service)
    monkeypatch.setattr(notifier_app, "build_notifier_preview", fake_build_notifier_preview)

    assert trader_app.main() == 0
    assert governor_app.main() == 0
    assert notifier_app.main() == 0

    components = original_build_trader_components()
    assert components["state_engine"].__class__.__name__ == "StateEngine"
    assert components["risk_kernel"].nav == 100_000.0
    assert components["checkpoint_service"].__class__.__name__ == "CheckpointService"
    assert components["client_order_id_builder"]("BTC-USDT-SWAP", "breakout", 1) == "BTC-USDT-SWAP-breakout-000001"

    assert original_build_governor_service().__class__.__name__ == "GovernorService"
    assert original_build_notifier_preview(RunMode.NORMAL) == "Mode changed to normal trading"
    assert trader_called == 1
    assert governor_called == 1
    assert notifier_called == 1

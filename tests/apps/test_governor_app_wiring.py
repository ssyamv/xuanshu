import xuanshu.apps.governor as governor_app


def test_governor_entrypoint_keeps_service_in_runtime(monkeypatch) -> None:
    build_called = 0
    seen_runtime = None

    async def _noop_wait_forever() -> None:
        return None

    async def fake_run_governor(runtime: governor_app.GovernorRuntime) -> None:
        nonlocal seen_runtime
        seen_runtime = runtime
        await _noop_wait_forever()

    monkeypatch.setattr(governor_app, "_run_governor", fake_run_governor)

    original_build_governor_service = governor_app.build_governor_service

    def fake_build_governor_service():
        nonlocal build_called
        build_called += 1
        return original_build_governor_service()

    monkeypatch.setattr(governor_app, "build_governor_service", fake_build_governor_service)

    assert governor_app.main() == 0
    assert build_called == 1
    assert seen_runtime is not None
    assert seen_runtime.service.__class__.__name__ == "GovernorService"

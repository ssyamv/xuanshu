import xuanshu.apps.notifier as notifier_app
from xuanshu.core.enums import RunMode


def test_notifier_entrypoint_keeps_runtime_boundary_silent(monkeypatch, capsys) -> None:
    build_called = 0
    seen_runtime = None

    async def _noop_wait_forever() -> None:
        return None

    async def fake_run_notifier(runtime: notifier_app.NotifierRuntime) -> None:
        nonlocal seen_runtime
        seen_runtime = runtime
        await _noop_wait_forever()

    monkeypatch.setattr(notifier_app, "_run_notifier", fake_run_notifier)

    original_build_notifier_runtime = notifier_app.build_notifier_runtime

    def fake_build_notifier_runtime(mode: RunMode = RunMode.NORMAL) -> notifier_app.NotifierRuntime:
        nonlocal build_called
        build_called += 1
        return original_build_notifier_runtime(mode)

    monkeypatch.setattr(notifier_app, "build_notifier_runtime", fake_build_notifier_runtime)

    assert notifier_app.main() == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert build_called == 1
    assert seen_runtime is not None
    assert seen_runtime.mode == RunMode.NORMAL

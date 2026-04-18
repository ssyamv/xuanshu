import xuanshu.apps.notifier as notifier_app
from xuanshu.apps.notifier import build_notifier_preview
from xuanshu.core.enums import RunMode


def test_notifier_entrypoint_emits_preview(monkeypatch, capsys) -> None:
    original_build_notifier_preview = build_notifier_preview
    build_called = 0

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(notifier_app, "_wait_forever", _noop_wait_forever)

    def fake_build_notifier_preview(mode: RunMode) -> str:
        nonlocal build_called
        build_called += 1
        return original_build_notifier_preview(mode)

    monkeypatch.setattr(notifier_app, "build_notifier_preview", fake_build_notifier_preview)

    assert notifier_app.main() == 0

    captured = capsys.readouterr()
    assert captured.out == "Mode changed to normal trading\n"
    assert original_build_notifier_preview(RunMode.NORMAL) == "Mode changed to normal trading"
    assert build_called == 1

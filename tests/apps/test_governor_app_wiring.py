import xuanshu.apps.governor as governor_app
from xuanshu.apps.governor import build_governor_service


def test_governor_entrypoint_builds_service(monkeypatch) -> None:
    original_build_governor_service = build_governor_service
    build_called = 0

    async def _noop_wait_forever() -> None:
        return None

    monkeypatch.setattr(governor_app, "_wait_forever", _noop_wait_forever)

    def fake_build_governor_service():
        nonlocal build_called
        build_called += 1
        return original_build_governor_service()

    monkeypatch.setattr(governor_app, "build_governor_service", fake_build_governor_service)

    assert governor_app.main() == 0
    assert original_build_governor_service().__class__.__name__ == "GovernorService"
    assert build_called == 1

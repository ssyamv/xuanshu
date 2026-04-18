from xuanshu.apps.governor import build_governor_service
from xuanshu.apps.notifier import build_notifier_preview


def test_service_entrypoints_exist() -> None:
    assert build_governor_service().__class__.__name__ == "GovernorService"
    assert build_notifier_preview("normal") == "Mode changed to normal"

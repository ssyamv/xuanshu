from xuanshu.notifier.service import format_mode_change


def test_mode_change_notification_is_human_readable() -> None:
    assert format_mode_change("reduce_only") == "Mode changed to reduce_only"

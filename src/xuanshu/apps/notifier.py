from xuanshu.notifier.service import format_mode_change


def build_notifier_preview(mode: str) -> str:
    return format_mode_change(mode)

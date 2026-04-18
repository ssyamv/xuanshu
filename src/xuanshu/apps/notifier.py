from xuanshu.core.enums import RunMode
from xuanshu.notifier.service import format_mode_change


def build_notifier_preview(mode: RunMode) -> str:
    return format_mode_change(mode)

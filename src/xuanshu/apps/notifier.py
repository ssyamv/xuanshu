from xuanshu.core.enums import RunMode


def build_notifier_preview(mode: RunMode | str) -> str:
    normalized_mode = mode.value if isinstance(mode, RunMode) else mode
    return f"Mode changed to {normalized_mode}"

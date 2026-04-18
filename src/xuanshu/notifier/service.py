from xuanshu.core.enums import RunMode


_MODE_LABELS: dict[RunMode, str] = {
    RunMode.NORMAL: "normal trading",
    RunMode.DEGRADED: "degraded trading",
    RunMode.REDUCE_ONLY: "reduce-only",
    RunMode.HALTED: "halted",
}


def format_mode_change(mode: RunMode) -> str:
    return f"Mode changed to {_MODE_LABELS[mode]}"

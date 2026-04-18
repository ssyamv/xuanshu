from __future__ import annotations

import re
from typing import Protocol


class RedisKeys:
    _SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")

    @staticmethod
    def latest_snapshot() -> str:
        return "xuanshu:strategy:latest"

    @staticmethod
    def run_mode() -> str:
        return "xuanshu:runtime:mode"

    @staticmethod
    def symbol_runtime(symbol: str) -> str:
        if not RedisKeys._SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(f"invalid runtime symbol: {symbol!r}")
        return f"xuanshu:runtime:symbol:{symbol}"


class SnapshotStore(Protocol):
    def set_latest_snapshot(self, version_id: str, snapshot: object) -> None:
        ...


class RedisSnapshotStore:
    def __init__(self) -> None:
        self.latest_version_id: str | None = None
        self.latest_snapshot: object | None = None

    def set_latest_snapshot(self, version_id: str, snapshot: object) -> None:
        self.latest_version_id = version_id
        self.latest_snapshot = snapshot

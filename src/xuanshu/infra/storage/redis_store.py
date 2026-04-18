from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol

from xuanshu.contracts.strategy import StrategyConfigSnapshot


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

    def get_latest_snapshot(self) -> StrategyConfigSnapshot | None:
        ...


class RedisSnapshotStore:
    def __init__(self, state_dir: str | Path | None = None) -> None:
        if state_dir is None:
            state_dir = Path(os.getenv("XUANSHU_SHARED_STATE_DIR", ".xuanshu-state"))
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_path = self.state_dir / "latest_strategy_snapshot.json"
        self.latest_version_id: str | None = None
        self.latest_snapshot: object | None = None

    def set_latest_snapshot(self, version_id: str, snapshot: object) -> None:
        self.latest_version_id = version_id
        self.latest_snapshot = snapshot
        if isinstance(snapshot, StrategyConfigSnapshot):
            self._snapshot_path.write_text(snapshot.model_dump_json(), encoding="utf-8")

    def get_latest_snapshot(self) -> StrategyConfigSnapshot | None:
        if self._snapshot_path.exists():
            return StrategyConfigSnapshot.model_validate_json(self._snapshot_path.read_text(encoding="utf-8"))
        if isinstance(self.latest_snapshot, StrategyConfigSnapshot):
            return self.latest_snapshot
        return None

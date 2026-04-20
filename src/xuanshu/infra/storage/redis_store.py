from __future__ import annotations

import json
import re
from typing import Protocol

from pydantic import ValidationError
from redis import Redis
from redis.exceptions import RedisError

from xuanshu.core.enums import RunMode
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

    @staticmethod
    def budget_pool_summary() -> str:
        return "xuanshu:runtime:budget_pool"

    @staticmethod
    def fault_flags() -> str:
        return "xuanshu:runtime:fault_flags"

    @staticmethod
    def governor_health_summary() -> str:
        return "xuanshu:runtime:governor_health"

    @staticmethod
    def manual_release_target() -> str:
        return "xuanshu:runtime:manual_release_target"

    @staticmethod
    def pending_approval_summary() -> str:
        return "xuanshu:runtime:pending_approval_summary"

    @staticmethod
    def latest_approved_package_summary() -> str:
        return "xuanshu:runtime:latest_approved_package_summary"

    @staticmethod
    def backtest_health_summary() -> str:
        return "xuanshu:runtime:backtest_health_summary"


class SnapshotStore(Protocol):
    def set_latest_snapshot(self, version_id: str, snapshot: StrategyConfigSnapshot) -> None:
        ...

    def get_latest_snapshot(self) -> StrategyConfigSnapshot | None:
        ...


class RuntimeStateStore(Protocol):
    def set_run_mode(self, mode: RunMode) -> None:
        ...

    def get_run_mode(self) -> RunMode | None:
        ...

    def set_symbol_runtime_summary(self, symbol: str, summary: dict[str, object]) -> None:
        ...

    def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
        ...

    def set_fault_flags(self, flags: dict[str, object]) -> None:
        ...

    def get_fault_flags(self) -> dict[str, object] | None:
        ...

    def set_budget_pool_summary(self, summary: dict[str, object]) -> None:
        ...

    def get_budget_pool_summary(self) -> dict[str, object] | None:
        ...

    def set_governor_health_summary(self, summary: dict[str, object]) -> None:
        ...

    def get_governor_health_summary(self) -> dict[str, object] | None:
        ...

    def set_pending_approval_summary(self, summary: dict[str, object]) -> None:
        ...

    def get_pending_approval_summary(self) -> dict[str, object] | None:
        ...

    def set_latest_approved_package_summary(self, summary: dict[str, object]) -> None:
        ...

    def get_latest_approved_package_summary(self) -> dict[str, object] | None:
        ...

    def set_backtest_health_summary(self, summary: dict[str, object]) -> None:
        ...

    def get_backtest_health_summary(self) -> dict[str, object] | None:
        ...

    def set_manual_release_target(self, mode: str) -> None:
        ...

    def get_manual_release_target(self) -> str | None:
        ...

    def clear_manual_release_target(self) -> None:
        ...


class RedisSnapshotStore:
    def __init__(
        self,
        redis_url: str = "redis://redis:6379/0",
        redis_client: Redis | object | None = None,
    ) -> None:
        self._redis = redis_client or Redis.from_url(redis_url)
        self._key = RedisKeys.latest_snapshot()
        self.latest_version_id: str | None = None
        self.latest_snapshot: StrategyConfigSnapshot | None = None

    def set_latest_snapshot(self, version_id: str, snapshot: StrategyConfigSnapshot) -> None:
        self.latest_version_id = version_id
        self.latest_snapshot = snapshot
        try:
            self._redis.set(self._key, snapshot.model_dump_json())
        except RedisError:
            return

    def get_latest_snapshot(self) -> StrategyConfigSnapshot | None:
        try:
            payload = self._redis.get(self._key)
        except RedisError:
            return self.latest_snapshot
        if payload is None:
            return self.latest_snapshot
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return self.latest_snapshot
        if not isinstance(payload, str):
            return self.latest_snapshot
        try:
            snapshot = StrategyConfigSnapshot.model_validate_json(payload)
        except (ValidationError, UnicodeDecodeError, ValueError):
            return self.latest_snapshot
        self.latest_version_id = snapshot.version_id
        self.latest_snapshot = snapshot
        return snapshot


class RedisRuntimeStateStore:
    def __init__(
        self,
        redis_url: str = "redis://redis:6379/0",
        redis_client: Redis | object | None = None,
    ) -> None:
        self._redis = redis_client or Redis.from_url(redis_url)
        self._key = RedisKeys.run_mode()
        self._manual_release_key = RedisKeys.manual_release_target()
        self._latest_mode: RunMode | None = None
        self._latest_manual_release_target: str | None = None

    def set_run_mode(self, mode: RunMode) -> None:
        self._latest_mode = mode
        try:
            self._redis.set(self._key, mode.value)
        except RedisError:
            return

    def get_run_mode(self) -> RunMode | None:
        try:
            payload = self._redis.get(self._key)
        except RedisError:
            return self._latest_mode
        if payload is None:
            return self._latest_mode
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return self._latest_mode
        if not isinstance(payload, str) or not payload:
            return self._latest_mode
        try:
            mode = RunMode(payload)
        except ValueError:
            return self._latest_mode
        self._latest_mode = mode
        return mode

    def set_symbol_runtime_summary(self, symbol: str, summary: dict[str, object]) -> None:
        try:
            self._redis.set(RedisKeys.symbol_runtime(symbol), json.dumps(summary, separators=(",", ":")))
        except RedisError:
            return

    def get_symbol_runtime_summary(self, symbol: str) -> dict[str, object] | None:
        try:
            payload = self._redis.get(RedisKeys.symbol_runtime(symbol))
        except RedisError:
            return None
        if payload is None:
            return None
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(payload, str):
            return None
        try:
            summary = json.loads(payload)
        except (TypeError, ValueError):
            return None
        return summary if isinstance(summary, dict) else None

    def set_fault_flags(self, flags: dict[str, object]) -> None:
        try:
            self._redis.set(RedisKeys.fault_flags(), json.dumps(flags, separators=(",", ":")))
        except RedisError:
            return

    def get_fault_flags(self) -> dict[str, object] | None:
        try:
            payload = self._redis.get(RedisKeys.fault_flags())
        except RedisError:
            return None
        if payload is None:
            return None
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(payload, str):
            return None
        try:
            flags = json.loads(payload)
        except (TypeError, ValueError):
            return None
        return flags if isinstance(flags, dict) else None

    def set_budget_pool_summary(self, summary: dict[str, object]) -> None:
        try:
            self._redis.set(RedisKeys.budget_pool_summary(), json.dumps(summary, separators=(",", ":")))
        except RedisError:
            return

    def get_budget_pool_summary(self) -> dict[str, object] | None:
        try:
            payload = self._redis.get(RedisKeys.budget_pool_summary())
        except RedisError:
            return None
        if payload is None:
            return None
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(payload, str):
            return None
        try:
            summary = json.loads(payload)
        except (TypeError, ValueError):
            return None
        return summary if isinstance(summary, dict) else None

    def set_governor_health_summary(self, summary: dict[str, object]) -> None:
        try:
            self._redis.set(RedisKeys.governor_health_summary(), json.dumps(summary, separators=(",", ":")))
        except RedisError:
            return

    def get_governor_health_summary(self) -> dict[str, object] | None:
        try:
            payload = self._redis.get(RedisKeys.governor_health_summary())
        except RedisError:
            return None
        if payload is None:
            return None
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(payload, str):
            return None
        try:
            summary = json.loads(payload)
        except (TypeError, ValueError):
            return None
        return summary if isinstance(summary, dict) else None

    def set_pending_approval_summary(self, summary: dict[str, object]) -> None:
        try:
            self._redis.set(
                RedisKeys.pending_approval_summary(),
                json.dumps(summary, separators=(",", ":")),
            )
        except RedisError:
            return

    def get_pending_approval_summary(self) -> dict[str, object] | None:
        try:
            payload = self._redis.get(RedisKeys.pending_approval_summary())
        except RedisError:
            return None
        if payload is None:
            return None
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(payload, str):
            return None
        try:
            summary = json.loads(payload)
        except (TypeError, ValueError):
            return None
        return summary if isinstance(summary, dict) else None

    def set_latest_approved_package_summary(self, summary: dict[str, object]) -> None:
        try:
            self._redis.set(
                RedisKeys.latest_approved_package_summary(),
                json.dumps(summary, separators=(",", ":")),
            )
        except RedisError:
            return

    def get_latest_approved_package_summary(self) -> dict[str, object] | None:
        try:
            payload = self._redis.get(RedisKeys.latest_approved_package_summary())
        except RedisError:
            return None
        if payload is None:
            return None
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(payload, str):
            return None
        try:
            summary = json.loads(payload)
        except (TypeError, ValueError):
            return None
        return summary if isinstance(summary, dict) else None

    def set_backtest_health_summary(self, summary: dict[str, object]) -> None:
        try:
            self._redis.set(
                RedisKeys.backtest_health_summary(),
                json.dumps(summary, separators=(",", ":")),
            )
        except RedisError:
            return

    def get_backtest_health_summary(self) -> dict[str, object] | None:
        try:
            payload = self._redis.get(RedisKeys.backtest_health_summary())
        except RedisError:
            return None
        if payload is None:
            return None
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(payload, str):
            return None
        try:
            summary = json.loads(payload)
        except (TypeError, ValueError):
            return None
        return summary if isinstance(summary, dict) else None

    def set_manual_release_target(self, mode: str) -> None:
        normalized = str(mode).strip()
        self._latest_manual_release_target = normalized or None
        try:
            if self._latest_manual_release_target is None:
                self._redis.delete(self._manual_release_key)
            else:
                self._redis.set(self._manual_release_key, self._latest_manual_release_target)
        except RedisError:
            return

    def get_manual_release_target(self) -> str | None:
        try:
            payload = self._redis.get(self._manual_release_key)
        except RedisError:
            return self._latest_manual_release_target
        if payload is None:
            return self._latest_manual_release_target
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return self._latest_manual_release_target
        if not isinstance(payload, str):
            return self._latest_manual_release_target
        normalized = payload.strip()
        self._latest_manual_release_target = normalized or None
        return self._latest_manual_release_target

    def clear_manual_release_target(self) -> None:
        self._latest_manual_release_target = None
        try:
            self._redis.delete(self._manual_release_key)
        except RedisError:
            return

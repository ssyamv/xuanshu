from __future__ import annotations

from dataclasses import dataclass

import httpx
from pydantic import ValidationError
from redis import Redis
from sqlalchemy import create_engine, text

from xuanshu.config.settings import (
    GovernorRuntimeSettings,
    NotifierRuntimeSettings,
    Settings,
    TraderRuntimeSettings,
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def check_trader_runtime(settings: TraderRuntimeSettings) -> CheckResult:
    return CheckResult(
        "trader_runtime",
        True,
        f"ok default_run_mode={settings.default_run_mode.value} symbols={len(settings.okx_symbols)}",
    )


def check_governor_runtime(settings: GovernorRuntimeSettings) -> CheckResult:
    return CheckResult(
        "governor_runtime",
        True,
        f"ok research_provider={settings.research_provider.value}",
    )


def check_notifier_runtime(settings: NotifierRuntimeSettings) -> CheckResult:
    return CheckResult(
        "notifier_runtime",
        True,
        f"ok chat_id={settings.telegram_chat_id}",
    )


def check_redis(redis_url: str) -> CheckResult:
    try:
        client = Redis.from_url(redis_url)
        client.ping()
    except Exception as exc:
        return CheckResult("redis", False, str(exc))
    return CheckResult("redis", True, "ok")


def check_postgres(postgres_dsn: str) -> CheckResult:
    try:
        engine = create_engine(postgres_dsn, future=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:
        return CheckResult("postgres", False, str(exc))
    return CheckResult("postgres", True, "ok")


def check_qdrant(qdrant_url: str) -> CheckResult:
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{qdrant_url.rstrip('/')}/healthz")
            response.raise_for_status()
    except Exception as exc:
        return CheckResult("qdrant", False, str(exc))
    return CheckResult("qdrant", True, "ok")


def run_preflight() -> list[CheckResult]:
    settings = Settings()
    trader = TraderRuntimeSettings()
    governor = GovernorRuntimeSettings()
    notifier = NotifierRuntimeSettings()

    results = [CheckResult("settings", True, f"ok env={settings.env} default_run_mode={trader.default_run_mode.value}")]
    results.append(check_trader_runtime(trader))
    results.append(check_governor_runtime(governor))
    results.append(check_notifier_runtime(notifier))
    results.append(check_redis(str(trader.redis_url)))
    results.append(check_postgres(str(trader.postgres_dsn)))
    results.append(check_qdrant(str(settings.qdrant_url)))
    return results


def main() -> int:
    try:
        results = run_preflight()
    except ValidationError as exc:
        print(f"settings: FAIL {exc}")
        return 1
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"{result.name}: {status} {result.detail}")
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

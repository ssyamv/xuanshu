#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(os.environ.get("XUANSHU_ROOT", "/opt/xuanshu"))
LOG_PREFIX = "[xuanshu-health]"


def run(command: list[str], *, timeout: int = 30) -> tuple[int, str, str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def compose(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    return run(["docker", "compose", "--env-file", ".env.prod", *args], timeout=timeout)


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def status_icon(ok: bool) -> str:
    return "OK" if ok else "异常"


def collect_compose_status() -> tuple[bool, list[str]]:
    code, output, error = compose("ps", "--format", "json", timeout=20)
    if code != 0:
        return False, [f"compose ps 失败: {error or output}"]

    expected = {"trader", "notifier", "dashboard", "redis", "postgres"}
    seen: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        service = str(item.get("Service") or "")
        state = str(item.get("State") or item.get("Status") or "")
        health = str(item.get("Health") or "")
        seen[service] = f"{state}{'/' + health if health else ''}"

    lines = []
    ok = True
    for service in sorted(expected):
        state = seen.get(service)
        service_ok = state is not None and "running" in state.lower()
        if service in {"redis", "postgres"} and state:
            service_ok = service_ok and "healthy" in state.lower()
        ok = ok and service_ok
        lines.append(f"{service}: {status_icon(service_ok)} ({state or 'missing'})")
    return ok, lines


def collect_redis_state() -> tuple[bool, list[str]]:
    code, output, error = compose(
        "exec",
        "-T",
        "redis",
        "redis-cli",
        "MGET",
        "xuanshu:runtime:mode",
        "xuanshu:runtime:fault_flags",
        "xuanshu:runtime:budget_pool",
        timeout=20,
    )
    if code != 0:
        return False, [f"Redis 检查失败: {error or output}"]
    values = output.splitlines()
    mode = values[0] if len(values) > 0 and values[0] else "unknown"
    fault_flags = values[1] if len(values) > 1 and values[1] else "{}"
    budget = {}
    if len(values) > 2 and values[2]:
        try:
            budget = json.loads(values[2])
        except json.JSONDecodeError:
            budget = {}
    ok = mode == "normal" and fault_flags in {"{}", "null"}
    lines = [
        f"运行模式: {mode}",
        f"故障标记: {fault_flags}",
    ]
    if budget:
        lines.append(f"权益: {float(budget.get('equity', 0.0)):.2f}")
        lines.append(f"策略资金: {float(budget.get('strategy_total_amount', 0.0)):.2f}")
    return ok, lines


def collect_dashboard() -> tuple[bool, list[str]]:
    code, output, error = compose(
        "exec",
        "-T",
        "dashboard",
        "python",
        "-c",
        (
            "import urllib.request;"
            "r=urllib.request.urlopen('http://127.0.0.1:8000/xuanshu/healthz',timeout=5);"
            "print(r.status);print(r.read().decode())"
        ),
        timeout=15,
    )
    if code != 0:
        return False, [f"Dashboard 检查失败: {error or output}"]
    parts = output.splitlines()
    status = parts[0] if parts else "unknown"
    body = json.loads(parts[1]) if len(parts) > 1 else {}
    ok = status == "200" and body.get("redis") is True and body.get("postgres") is True
    return ok, [f"Dashboard: HTTP {status}", f"Redis/Postgres: {body.get('redis')}/{body.get('postgres')}"]


def collect_recent_errors() -> tuple[bool, list[str]]:
    code, output, error = compose(
        "logs",
        "--since=30m",
        "trader",
        "notifier",
        "dashboard",
        timeout=30,
    )
    if code != 0:
        return False, [f"日志检查失败: {error or output}"]
    needles = ("ERROR", "CRITICAL", "Traceback", "runtime_failed", "halted", "degraded", "reduce_only")
    hits = [line for line in output.splitlines() if any(needle in line for needle in needles)]
    if hits:
        return False, [f"最近30分钟关键日志异常: {len(hits)} 条", *hits[-3:]]
    return True, ["最近30分钟关键日志异常: 0 条"]


def collect_risk_events() -> tuple[bool, list[str]]:
    sql = (
        "select count(*) filter (where created_at > now() - interval '30 minutes') as risk_events_last_30m, "
        "max(created_at) filter (where payload->>'event_type'='execution_submission_failed') as last_execution_failure, "
        "max(created_at) filter (where payload->>'event_type'='startup_recovery_failed') as last_startup_failure "
        "from risk_events;"
    )
    code, output, error = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "xuanshu",
        "-d",
        "xuanshu",
        "-At",
        "-F",
        "|",
        "-c",
        sql,
        timeout=30,
    )
    if code != 0:
        return False, [f"风险事件查询失败: {error or output}"]
    fields = (output.splitlines()[-1] if output else "||").split("|")
    recent_count = int(fields[0] or 0)
    ok = recent_count == 0
    return ok, [
        f"最近30分钟风险事件: {recent_count}",
        f"最近下单失败: {fields[1] or '无'}",
        f"最近启动恢复失败: {fields[2] or '无'}",
    ]


def collect_okx_state() -> tuple[bool, list[str]]:
    probe = r"""
import asyncio
from datetime import UTC, datetime
from xuanshu.config.settings import TraderRuntimeSettings
from xuanshu.apps.trader import build_trader_components

async def main():
    settings = TraderRuntimeSettings()
    components = build_trader_components(settings)
    try:
        ts = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        account = await components.okx_rest_client.fetch_account_summary(ts)
        result = {"account_mode": settings.okx_account_mode.value, "account": account[:1], "symbols": []}
        for symbol in settings.okx_symbols:
            ts = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            positions = await components.okx_rest_client.fetch_positions(symbol, ts)
            ts = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            orders = await components.okx_rest_client.fetch_open_orders(symbol, ts)
            result["symbols"].append({"symbol": symbol, "positions": positions, "open_orders": len(orders)})
        print(__import__("json").dumps(result, ensure_ascii=False))
    finally:
        await components.aclose()

asyncio.run(main())
"""
    code, output, error = compose("exec", "-T", "trader", "python", "-c", probe, timeout=30)
    if code != 0:
        return False, [f"OKX 实时检查失败: {error or output}"]
    data = json.loads(output.splitlines()[-1])
    lines = [f"OKX账户模式: {data.get('account_mode')}"]
    account = (data.get("account") or [{}])[0]
    if account:
        lines.append(f"OKX权益: {float(account.get('totalEq') or 0.0):.2f}")
    ok = True
    for item in data.get("symbols", []):
        active = [p for p in item.get("positions", []) if float(p.get("pos") or 0.0) != 0.0]
        summary = ", ".join(
            f"{p.get('posSide')} {p.get('pos')}@{p.get('avgPx')} mark={p.get('markPx')} upl={p.get('upl')}"
            for p in active
        ) or "无持仓"
        open_orders = int(item.get("open_orders") or 0)
        lines.append(f"{item.get('symbol')}: {summary}; 挂单={open_orders}")
    return ok, lines


def send_telegram(message: str) -> None:
    env = load_env(ROOT / ".env.prod")
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read().decode("utf-8", "replace")
        if response.status != 200:
            raise RuntimeError(f"Telegram send failed: HTTP {response.status} {body}")


def main() -> int:
    checks = [
        ("容器", collect_compose_status),
        ("Redis运行态", collect_redis_state),
        ("Dashboard", collect_dashboard),
        ("近期日志", collect_recent_errors),
        ("风险事件", collect_risk_events),
        ("OKX实时状态", collect_okx_state),
    ]
    sections: list[str] = []
    all_ok = True
    for title, check in checks:
        try:
            ok, lines = check()
        except Exception as exc:
            ok, lines = False, [f"{type(exc).__name__}: {exc}"]
        all_ok = all_ok and ok
        sections.append(f"{status_icon(ok)} {title}\n" + "\n".join(f"- {line}" for line in lines))

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    message = "\n\n".join(
        [
            f"玄枢每日运行检查：{status_icon(all_ok)}",
            f"时间：{now}",
            *sections,
        ]
    )
    print(f"{LOG_PREFIX} sending telegram report, status={status_icon(all_ok)}")
    send_telegram(message[:3900])
    print(f"{LOG_PREFIX} report sent")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

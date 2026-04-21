import pytest

from xuanshu.ops import preflight


def _set_required_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XUANSHU_DEFAULT_RUN_MODE", "halted")
    monkeypatch.setenv("OKX_API_KEY", "okx-key")
    monkeypatch.setenv("OKX_API_SECRET", "okx-secret")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "okx-passphrase")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql+psycopg://xuanshu:xuanshu@localhost:5432/xuanshu")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")


def test_run_preflight_returns_success_when_all_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_runtime_env(monkeypatch)
    monkeypatch.setattr(preflight, "check_redis", lambda _: preflight.CheckResult("redis", True, "ok"))
    monkeypatch.setattr(preflight, "check_postgres", lambda _: preflight.CheckResult("postgres", True, "ok"))

    results = preflight.run_preflight()

    assert [result.name for result in results] == [
        "settings",
        "trader_runtime",
        "notifier_runtime",
        "redis",
        "postgres",
    ]
    assert all(result.ok for result in results)
    assert "default_run_mode=halted" in results[0].detail
    assert results[1].detail == "ok default_run_mode=halted symbols=2"
    assert results[2].detail == "ok chat_id=123456"


def test_preflight_main_returns_nonzero_when_a_dependency_check_fails(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _set_required_runtime_env(monkeypatch)
    monkeypatch.setattr(preflight, "check_redis", lambda _: preflight.CheckResult("redis", True, "ok"))
    monkeypatch.setattr(preflight, "check_postgres", lambda _: preflight.CheckResult("postgres", False, "down"))

    exit_code = preflight.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "postgres: FAIL down" in captured.out


def test_preflight_main_returns_nonzero_when_runtime_configuration_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_required_runtime_env(monkeypatch)
    monkeypatch.delenv("TELEGRAM_CHAT_ID")

    exit_code = preflight.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "settings: FAIL" in captured.out


def test_run_preflight_ignores_removed_ai_and_qdrant_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_runtime_env(monkeypatch)
    monkeypatch.setenv("XUANSHU_RESEARCH_PROVIDER", "codex_cli")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setattr(preflight, "check_redis", lambda _: preflight.CheckResult("redis", True, "ok"))
    monkeypatch.setattr(preflight, "check_postgres", lambda _: preflight.CheckResult("postgres", True, "ok"))

    results = preflight.run_preflight()

    assert all(result.ok for result in results)
    assert [result.name for result in results] == [
        "settings",
        "trader_runtime",
        "notifier_runtime",
        "redis",
        "postgres",
    ]

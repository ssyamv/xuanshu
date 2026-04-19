from datetime import UTC, datetime, timedelta, timezone
from subprocess import CompletedProcess

import pytest
from pydantic import SecretStr

import xuanshu.governor.research_providers as research_providers_module
from xuanshu.contracts.research import ResearchTrigger
from xuanshu.governor.research import StrategyResearchEngine
from xuanshu.governor.research_providers import (
    ApiResearchProvider,
    CodexCliResearchProvider,
    ResearchProviderName,
    create_research_provider,
)


def test_strategy_research_engine_builds_candidate_package_from_history() -> None:
    engine = StrategyResearchEngine()

    package = engine.build_candidate_package(
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
            {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 103.0},
        ],
        research_reason="manual trend study",
    )

    assert package.strategy_family == "breakout"
    assert package.symbol_scope == ["BTC-USDT-SWAP"]
    assert package.market_environment_scope == ["trend"]
    assert package.backtest_summary == {
        "row_count": 2,
        "start_close": 100.0,
        "end_close": 103.0,
        "close_change_bps": 300.0,
    }
    assert package.performance_summary == {"return_percent": 3.0}


@pytest.mark.asyncio
async def test_strategy_research_engine_builds_candidate_package_from_provider_analysis() -> None:
    class _Provider:
        provider_name = ResearchProviderName.API

        async def generate_analysis(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return research_providers_module.ResearchProviderSuggestion(
                thesis="trend continuation remains intact",
                strategy_family="breakout",
                entry_signal="provider_breakout_signal",
                exit_stop_loss_bps=65,
                exit_take_profit_bps=150,
                risk_fraction=0.003,
                max_hold_minutes=90,
                failure_modes=["whipsaw"],
                invalidating_conditions=["trend breakdown"],
            )

    engine = StrategyResearchEngine(provider=_Provider())

    package = await engine.build_candidate_package_from_provider(
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
            {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 103.0},
        ],
        research_reason="manual trend study",
    )

    assert package.strategy_family == "breakout"
    assert package.entry_rules == {"signal": "provider_breakout_signal"}
    assert package.exit_rules == {"stop_loss_bps": 65, "take_profit_bps": 150}
    assert package.position_sizing_rules == {"risk_fraction": 0.003}
    assert package.risk_constraints == {"max_hold_minutes": 90}
    assert package.failure_modes == ["whipsaw"]
    assert package.invalidating_conditions == ["trend breakdown"]
    assert package.research_reason == "manual trend study | trend continuation remains intact"


def test_strategy_research_engine_canonicalizes_equivalent_inputs_into_same_package_id() -> None:
    engine = StrategyResearchEngine()

    ordered_rows = [
        {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
        {"timestamp": datetime(2026, 4, 19, 8, 5, tzinfo=timezone(timedelta(hours=8))), "close": 103.0},
    ]
    reordered_equivalent_rows = [
        {"timestamp": datetime(2026, 4, 19, 8, 5, tzinfo=timezone(timedelta(hours=8))), "close": 103.0},
        {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
    ]

    package_a = engine.build_candidate_package(
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=[" BTC-USDT-SWAP "],
        market_environment="trend",
        historical_rows=ordered_rows,
        research_reason=" manual trend study ",
    )
    package_b = engine.build_candidate_package(
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=reordered_equivalent_rows,
        research_reason="manual trend study",
    )

    assert package_a.strategy_package_id == package_b.strategy_package_id
    assert package_a.symbol_scope == ["BTC-USDT-SWAP"]
    assert package_a.generated_at == datetime(2026, 4, 19, 0, 5, tzinfo=UTC)
    assert package_a.backtest_summary == package_b.backtest_summary == {
        "row_count": 2,
        "start_close": 100.0,
        "end_close": 103.0,
        "close_change_bps": 300.0,
    }
    assert package_a.performance_summary == package_b.performance_summary == {"return_percent": 3.0}


@pytest.mark.asyncio
async def test_api_research_provider_posts_prompt_and_parses_json(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "output_text": """```json
{
  "thesis": "trend remains constructive",
  "strategy_family": "breakout",
  "entry_signal": "breakout_confirmed",
  "exit_stop_loss_bps": 55,
  "exit_take_profit_bps": 140,
  "risk_fraction": 0.002,
  "max_hold_minutes": 75,
  "failure_modes": ["range chop"],
  "invalidating_conditions": ["volatility collapse"]
}
```"""
            }

    class _AsyncClient:
        def __init__(self, *, timeout: float, headers: dict[str, str]) -> None:
            captured["timeout"] = timeout
            captured["headers"] = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, json: dict[str, object]) -> _Response:
            captured["url"] = url
            captured["payload"] = json
            return _Response()

    monkeypatch.setattr(research_providers_module.httpx, "AsyncClient", _AsyncClient)

    provider = ApiResearchProvider(api_key=SecretStr("openai-key"), timeout_sec=9)
    suggestion = await provider.generate_analysis(
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
            {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 103.0},
        ],
        research_reason="manual trend study",
    )

    assert suggestion.thesis == "trend remains constructive"
    assert suggestion.strategy_family == "breakout"
    assert captured["timeout"] == 9
    assert captured["headers"]["Authorization"] == "Bearer openai-key"
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert "Research context JSON" in captured["payload"]["input"][1]["content"][0]["text"]


@pytest.mark.asyncio
async def test_codex_cli_research_provider_invokes_codex_exec(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run(cmd: list[str], *, capture_output: bool, text: bool, check: bool, cwd: str | None) -> CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["capture_output"] = capture_output
        captured["text"] = text
        captured["check"] = check
        captured["cwd"] = cwd
        return CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="""```json
{
  "thesis": "codex trend thesis",
  "strategy_family": "breakout",
  "entry_signal": "codex_breakout",
  "exit_stop_loss_bps": 45,
  "exit_take_profit_bps": 125,
  "risk_fraction": 0.0025,
  "max_hold_minutes": 60,
  "failure_modes": ["late entry"],
  "invalidating_conditions": ["regime shift"]
}
```""",
            stderr="",
        )

    monkeypatch.setattr(research_providers_module.subprocess, "run", _fake_run)

    provider = CodexCliResearchProvider(command="codex", cwd="/tmp/xuanshu")
    suggestion = await provider.generate_analysis(
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
            {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 103.0},
        ],
        research_reason="manual trend study",
    )

    assert suggestion.entry_signal == "codex_breakout"
    assert captured["cmd"][:3] == ["codex", "exec", "--skip-git-repo-check"]
    assert captured["cwd"] == "/tmp/xuanshu"
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["check"] is False
    prompt = captured["cmd"][3]
    assert '"thesis": string' in prompt
    assert '"strategy_family": string' in prompt
    assert '"entry_signal": string' in prompt
    assert '"exit_stop_loss_bps": integer' in prompt
    assert '"exit_take_profit_bps": integer' in prompt
    assert '"risk_fraction": number' in prompt
    assert '"max_hold_minutes": integer' in prompt
    assert '"failure_modes": string[]' in prompt
    assert '"invalidating_conditions": string[]' in prompt
    assert "Do not return keys outside this schema." in prompt


def test_create_research_provider_rejects_unsupported_provider() -> None:
    with pytest.raises(ValueError, match="unsupported research provider"):
        create_research_provider(
            provider_name="chatgpt_pro_web",
            openai_api_key=SecretStr("openai-key"),
            timeout_sec=9,
        )


def test_strategy_research_engine_rejects_mixed_aware_and_naive_timestamps() -> None:
    engine = StrategyResearchEngine()

    with pytest.raises(ValueError, match="timezone-aware"):
        engine.build_candidate_package(
            trigger=ResearchTrigger.MANUAL,
            symbol_scope=["BTC-USDT-SWAP"],
            market_environment="trend",
            historical_rows=[
                {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
                {"timestamp": datetime(2026, 4, 19, 0, 5), "close": 103.0},
            ],
            research_reason="manual trend study",
        )


def test_strategy_research_engine_rejects_rows_without_timestamps() -> None:
    engine = StrategyResearchEngine()

    with pytest.raises(ValueError, match="timestamp"):
        engine.build_candidate_package(
            trigger=ResearchTrigger.MANUAL,
            symbol_scope=["BTC-USDT-SWAP"],
            market_environment="trend",
            historical_rows=[
                {"close": 100.0},
                {"close": 103.0},
            ],
            research_reason="manual trend study",
        )


@pytest.mark.parametrize(
    ("kwargs", "error_message"),
    [
        (
            {
                "symbol_scope": [],
                "market_environment": "trend",
                "historical_rows": [{"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0}],
                "research_reason": "manual trend study",
            },
            "symbol_scope must not be blank",
        ),
        (
            {
                "symbol_scope": ["   "],
                "market_environment": "trend",
                "historical_rows": [{"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0}],
                "research_reason": "manual trend study",
            },
            "symbol_scope must not be blank",
        ),
        (
            {
                "symbol_scope": ["BTC-USDT-SWAP"],
                "market_environment": "   ",
                "historical_rows": [{"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0}],
                "research_reason": "manual trend study",
            },
            "market_environment must not be blank",
        ),
        (
            {
                "symbol_scope": ["BTC-USDT-SWAP"],
                "market_environment": "trend",
                "historical_rows": [{"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0}],
                "research_reason": "   ",
            },
            "research_reason must not be blank",
        ),
        (
            {
                "symbol_scope": ["BTC-USDT-SWAP"],
                "market_environment": "trend",
                "historical_rows": [],
                "research_reason": "manual trend study",
            },
            "historical_rows must not be empty",
        ),
    ],
)
def test_strategy_research_engine_rejects_blank_and_empty_inputs(
    kwargs: dict[str, object],
    error_message: str,
) -> None:
    engine = StrategyResearchEngine()

    with pytest.raises(ValueError, match=error_message):
        engine.build_candidate_package(
            trigger=ResearchTrigger.MANUAL,
            **kwargs,
        )

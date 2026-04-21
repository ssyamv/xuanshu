from datetime import UTC, datetime, timedelta, timezone
from subprocess import CompletedProcess

import pytest
from pydantic import SecretStr, ValidationError

import xuanshu.governor.research_providers as research_providers_module
from xuanshu.contracts.research import ResearchTrigger
from xuanshu.governor.research import StrategyResearchEngine
from xuanshu.governor.research_providers import (
    ApiResearchProvider,
    CodexCliResearchProvider,
    ResearchProviderName,
    ResearchProviderSuggestion,
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
    assert package.score == 3.0
    assert package.strategy_definition.score == 3.0
    assert any(
        rule.get("op") == "time_stop_minutes" and rule.get("value") == 60
        for rule in package.strategy_definition.exit_rules["any"]
    )


def test_strategy_research_engine_clamps_negative_return_score_to_zero() -> None:
    engine = StrategyResearchEngine()

    package = engine.build_candidate_package(
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
            {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 90.0},
        ],
        research_reason="manual trend study",
    )

    assert package.performance_summary["return_percent"] < 0
    assert package.score == 0.0
    assert package.strategy_definition.score == 0.0


@pytest.mark.asyncio
async def test_strategy_research_engine_builds_candidate_package_from_provider_analysis() -> None:
    class _Provider:
        provider_name = ResearchProviderName.API

        async def generate_analyses(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return [
                research_providers_module.ResearchProviderSuggestion(
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
            ]

    engine = StrategyResearchEngine(provider=_Provider())

    packages = await engine.build_candidate_packages_from_provider(
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
            {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 103.0},
        ],
        research_reason="manual trend study",
    )

    assert len(packages) > 20
    assert any(package.strategy_family == "breakout" for package in packages)
    assert any(package.entry_rules == package.strategy_definition.entry_rules for package in packages)
    assert any(package.exit_rules == package.strategy_definition.exit_rules for package in packages)
    assert any(package.position_sizing_rules == {"risk_fraction": 0.003} for package in packages)
    assert any(package.risk_constraints == {"max_hold_minutes": 90} for package in packages)
    assert any(package.strategy_definition.position_sizing_rules == {"risk_fraction": 0.003} for package in packages)
    assert any(package.strategy_definition.risk_constraints == {"max_hold_minutes": 90} for package in packages)
    assert any(package.failure_modes == ["whipsaw"] for package in packages)
    assert any(package.invalidating_conditions == ["trend breakdown"] for package in packages)
    assert all(package.research_reason == "manual trend study | trend continuation remains intact" for package in packages)


@pytest.mark.asyncio
async def test_strategy_research_engine_uses_provider_strategy_definition_payload() -> None:
    explicit_definition = {
        "strategy_def_id": "ai-base-001",
        "symbol": "BTC-USDT-SWAP",
        "strategy_family": "breakout",
        "directionality": "long_only",
        "feature_spec": {
            "indicators": [
                {"name": "sma", "source": "close", "window": 20},
                {"name": "zscore", "source": "close", "window": 20},
            ]
        },
        "entry_rules": {
            "all": [
                {"op": "greater_than", "left": "zscore_20", "right": {"const": 1.5}},
            ]
        },
        "exit_rules": {
            "any": [
                {"op": "crosses_below", "left": "close", "right": "sma_20"},
                {"op": "take_profit_bps", "value": 150},
                {"op": "stop_loss_bps", "value": 65},
            ]
        },
        "position_sizing_rules": {"risk_fraction": 0.003, "custom_bias": "aggressive"},
        "risk_constraints": {"max_hold_minutes": 90},
        "parameter_set": {
            "lookback": 8,
            "signal_mode": "provider_breakout_signal",
            "stop_loss_bps": 65,
            "take_profit_bps": 150,
            "risk_fraction": 0.003,
            "max_hold_minutes": 90,
            "custom_bias": "aggressive",
        },
        "score": 12.5,
        "score_basis": "backtest_return_percent",
    }

    class _Provider:
        provider_name = ResearchProviderName.API

        async def generate_analyses(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return [
                research_providers_module.ResearchProviderSuggestion(
                    thesis="provider dsl remains intact",
                    strategy_family="breakout",
                    entry_signal="provider_breakout_signal",
                    exit_stop_loss_bps=65,
                    exit_take_profit_bps=150,
                    risk_fraction=0.003,
                    max_hold_minutes=90,
                    strategy_definition=explicit_definition,
                    failure_modes=["whipsaw"],
                    invalidating_conditions=["trend breakdown"],
                )
            ]

    engine = StrategyResearchEngine(provider=_Provider())

    packages = await engine.build_candidate_packages_from_provider(
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
            {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 103.0},
        ],
        research_reason="manual trend study",
    )

    assert len(packages) > 20
    assert len({package.strategy_package_id for package in packages}) == len(packages)
    assert any(package.strategy_definition.feature_spec == explicit_definition["feature_spec"] for package in packages)
    assert any(package.strategy_definition.entry_rules == explicit_definition["entry_rules"] for package in packages)
    assert any(package.strategy_definition.parameter_set["custom_bias"] == "aggressive" for package in packages)
    assert any(
        rule.get("op") == "time_stop_minutes" and rule.get("value") == 90
        for package in packages
        for rule in package.strategy_definition.exit_rules["any"]
    )
    assert all(package.strategy_definition.strategy_def_id == f"{package.strategy_package_id}-def" for package in packages)
    assert all(package.entry_rules == package.strategy_definition.entry_rules for package in packages)
    assert all(package.exit_rules == package.strategy_definition.exit_rules for package in packages)
    assert all(package.position_sizing_rules == package.strategy_definition.position_sizing_rules for package in packages)
    assert all(package.risk_constraints == package.strategy_definition.risk_constraints for package in packages)


@pytest.mark.asyncio
async def test_strategy_research_engine_synthesizes_time_stop_minutes_from_provider_definition() -> None:
    class _Provider:
        provider_name = ResearchProviderName.API

        async def generate_analyses(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return [
                research_providers_module.ResearchProviderSuggestion(
                    thesis="provider dsl omits time stop",
                    strategy_family="breakout",
                    entry_signal="provider_breakout_signal",
                    exit_stop_loss_bps=65,
                    exit_take_profit_bps=150,
                    risk_fraction=0.003,
                    max_hold_minutes=90,
                    strategy_definition={
                        "strategy_def_id": "ai-base-002",
                        "symbol": "BTC-USDT-SWAP",
                        "strategy_family": "breakout",
                        "directionality": "long_only",
                        "feature_spec": {
                            "indicators": [
                                {"name": "sma", "source": "close", "window": 20},
                            ]
                        },
                        "entry_rules": {
                            "all": [
                                {"op": "crosses_above", "left": "close", "right": "sma_20"},
                            ]
                        },
                        "exit_rules": {
                            "any": [
                                {"op": "crosses_below", "left": "close", "right": "sma_20"},
                                {"op": "take_profit_bps", "value": 150},
                                {"op": "stop_loss_bps", "value": 65},
                            ]
                        },
                        "position_sizing_rules": {"risk_fraction": 0.003},
                        "risk_constraints": {"max_hold_minutes": 90},
                        "parameter_set": {
                            "lookback": 8,
                            "signal_mode": "provider_breakout_signal",
                            "stop_loss_bps": 65,
                            "take_profit_bps": 150,
                            "risk_fraction": 0.003,
                            "max_hold_minutes": 90,
                        },
                        "score": 12.5,
                        "score_basis": "backtest_return_percent",
                    },
                    failure_modes=["whipsaw"],
                    invalidating_conditions=["trend breakdown"],
                )
            ]

    engine = StrategyResearchEngine(provider=_Provider())

    packages = await engine.build_candidate_packages_from_provider(
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
            {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 103.0},
        ],
        research_reason="manual trend study",
    )

    assert any(
        rule.get("op") == "time_stop_minutes" and rule.get("value") == 90
        for package in packages
        for rule in package.strategy_definition.exit_rules["any"]
    )
    assert all(package.exit_rules == package.strategy_definition.exit_rules for package in packages)
    assert all(
        any(
            rule.get("op") == "time_stop_minutes"
            and rule.get("value") == package.strategy_definition.risk_constraints["max_hold_minutes"]
            for rule in package.strategy_definition.exit_rules["any"]
        )
        for package in packages
    )
    assert all(package.strategy_definition.risk_constraints == package.risk_constraints for package in packages)


def test_parse_suggestion_payload_rejects_invalid_nested_strategy_definition() -> None:
    payload = """
    {
      "thesis": "invalid nested dsl",
      "strategy_family": "breakout",
      "entry_signal": "breakout_confirmed",
      "exit_stop_loss_bps": 55,
      "exit_take_profit_bps": 140,
      "risk_fraction": 0.002,
      "max_hold_minutes": 75,
      "strategy_definition": {
        "strategy_def_id": "ai-invalid-001",
        "symbol": "BTC-USDT-SWAP",
        "strategy_family": "breakout",
        "directionality": "long_only",
        "feature_spec": {"indicators": [{"name": "sma", "source": "close", "window": 20}]},
        "entry_rules": {"all": [{"op": "exec_python", "value": "boom"}]},
        "exit_rules": {"any": [{"op": "take_profit_bps", "value": 140}]},
        "position_sizing_rules": {"risk_fraction": 0.002},
        "risk_constraints": {"max_hold_minutes": 75},
        "parameter_set": {"lookback": 1},
        "score": 12.5,
        "score_basis": "backtest_return_percent"
      },
      "failure_modes": [],
      "invalidating_conditions": []
    }
    """

    with pytest.raises(ValidationError, match="unsupported operator"):
        research_providers_module._parse_suggestion_payload(payload)


@pytest.mark.asyncio
async def test_strategy_research_engine_builds_multiple_candidate_packages_from_provider_analysis() -> None:
    class _Provider:
        provider_name = ResearchProviderName.CODEX_CLI

        async def generate_analyses(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return [
                research_providers_module.ResearchProviderSuggestion(
                    thesis="first thesis",
                    strategy_family="breakout",
                    entry_signal="provider_breakout_signal",
                    exit_stop_loss_bps=65,
                    exit_take_profit_bps=150,
                    risk_fraction=0.003,
                    max_hold_minutes=90,
                    failure_modes=["whipsaw"],
                    invalidating_conditions=["trend breakdown"],
                ),
                research_providers_module.ResearchProviderSuggestion(
                    thesis="second thesis",
                    strategy_family="mean_reversion",
                    entry_signal="range_retest",
                    exit_stop_loss_bps=55,
                    exit_take_profit_bps=120,
                    risk_fraction=0.002,
                    max_hold_minutes=45,
                    failure_modes=["late reversal"],
                    invalidating_conditions=["trend acceleration"],
                ),
            ]

    engine = StrategyResearchEngine(provider=_Provider())

    packages = await engine.build_candidate_packages_from_provider(
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
            {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 103.0},
        ],
        research_reason="manual trend study",
    )

    assert len(packages) > 20
    assert len({package.strategy_package_id for package in packages}) == len(packages)
    assert any(package.research_reason.endswith("first thesis") for package in packages)
    assert any(package.research_reason.endswith("second thesis") for package in packages)
    assert any(package.strategy_family == "mean_reversion" for package in packages)
    assert max(package.position_sizing_rules["risk_fraction"] for package in packages) >= 0.1
    assert {package.directionality for package in packages} <= {"long_only", "short_only"}


@pytest.mark.asyncio
async def test_strategy_research_engine_normalizes_descriptive_provider_strategy_fields() -> None:
    class _Provider:
        provider_name = ResearchProviderName.CODEX_CLI

        async def generate_analyses(self, *, symbol_scope, market_environment, historical_rows, research_reason):
            return [
                research_providers_module.ResearchProviderSuggestion(
                    thesis="short-horizon downside continuation remains intact",
                    strategy_family="intraday trend-following pullback continuation",
                    entry_signal=(
                        "After a weak countertrend bounce, require price to fail to reclaim the prior "
                        "short-term swing area and then print a fresh local low."
                    ),
                    exit_stop_loss_bps=65,
                    exit_take_profit_bps=150,
                    risk_fraction=0.003,
                    max_hold_minutes=90,
                    failure_modes=["sharp reversal"],
                    invalidating_conditions=["structure break"],
                )
            ]

    engine = StrategyResearchEngine(provider=_Provider())

    packages = await engine.build_candidate_packages_from_provider(
        trigger=ResearchTrigger.SCHEDULE,
        symbol_scope=["ETH-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
            {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 103.0},
        ],
        research_reason="governor strategy research",
    )

    package = packages[0]
    assert package.strategy_family == "breakout"
    assert package.entry_rules == package.strategy_definition.entry_rules


@pytest.mark.asyncio
async def test_codex_cli_research_provider_invokes_codex_exec_and_parses_multiple_candidates(monkeypatch) -> None:
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
[
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
  },
  {
    "thesis": "codex reversion thesis",
    "strategy_family": "mean_reversion",
    "entry_signal": "range_retest",
    "exit_stop_loss_bps": 35,
    "exit_take_profit_bps": 90,
    "risk_fraction": 0.0015,
    "max_hold_minutes": 30,
    "failure_modes": ["trend extension"],
    "invalidating_conditions": ["breakout expansion"]
  }
]
```""",
            stderr="",
        )

    monkeypatch.setattr(research_providers_module.subprocess, "run", _fake_run)

    provider = CodexCliResearchProvider(command="codex", cwd="/tmp/xuanshu")
    suggestions = await provider.generate_analyses(
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
            {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 103.0},
        ],
        research_reason="manual trend study",
    )

    assert len(suggestions) == 2
    assert suggestions[0].entry_signal == "codex_breakout"
    assert suggestions[1].strategy_family == "mean_reversion"
    assert captured["cmd"][:3] == ["codex", "exec", "--skip-git-repo-check"]
    prompt = captured["cmd"][3]
    assert "JSON array containing between 8 and 20 diverse candidate strategies" in prompt
    assert "Return a JSON array" in prompt


@pytest.mark.asyncio
async def test_parse_suggestion_payload_accepts_single_object_or_array() -> None:
    single = research_providers_module._parse_suggestion_payload(
        """{"thesis":"one","strategy_family":"breakout","entry_signal":"breakout_confirmed","exit_stop_loss_bps":45,"exit_take_profit_bps":125,"risk_fraction":0.0025,"max_hold_minutes":60,"failure_modes":[],"invalidating_conditions":[]}"""
    )
    many = research_providers_module._parse_suggestion_payload(
        """[
        {"thesis":"one","strategy_family":"breakout","entry_signal":"breakout_confirmed","exit_stop_loss_bps":45,"exit_take_profit_bps":125,"risk_fraction":0.0025,"max_hold_minutes":60,"failure_modes":[],"invalidating_conditions":[]},
        {"thesis":"two","strategy_family":"mean_reversion","entry_signal":"range_retest","exit_stop_loss_bps":35,"exit_take_profit_bps":90,"risk_fraction":0.0015,"max_hold_minutes":30,"failure_modes":[],"invalidating_conditions":[]}
        ]"""
    )

    assert len(single) == 1
    assert isinstance(single[0], ResearchProviderSuggestion)
    assert len(many) == 2


@pytest.mark.asyncio
async def test_strategy_research_engine_builds_candidate_package_from_provider_analysis_legacy_single_method() -> None:
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
        trigger=ResearchTrigger.SCHEDULE,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=[
            {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
            {"timestamp": datetime(2026, 4, 19, 0, 5, tzinfo=UTC), "close": 103.0},
        ],
        research_reason="governor strategy research",
    )

    assert package.strategy_family == "breakout"


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
    assert "include no extra keys" in prompt


@pytest.mark.asyncio
async def test_codex_cli_research_provider_compacts_large_historical_context(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run(cmd: list[str], *, capture_output: bool, text: bool, check: bool, cwd: str | None) -> CompletedProcess[str]:
        captured["prompt"] = cmd[3]
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
    historical_rows = [
        {"timestamp": datetime(2026, 1, 1, 0, 0, tzinfo=UTC) + timedelta(hours=index), "close": 100.0 + index}
        for index in range(500)
    ]
    await provider.generate_analysis(
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment="trend",
        historical_rows=historical_rows,
        research_reason="manual trend study",
    )

    assert captured["prompt"].count('"timestamp"') <= 240


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

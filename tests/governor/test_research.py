from datetime import UTC, datetime, timedelta, timezone

import pytest

from xuanshu.contracts.research import ResearchTrigger
from xuanshu.governor.research import StrategyResearchEngine


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

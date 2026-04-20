from datetime import UTC, datetime, timedelta, timezone
import math

import pytest
from pydantic import ValidationError

from xuanshu.contracts.approval import ApprovalDecision, ApprovalRecord
from xuanshu.contracts.backtest import BacktestReport
from xuanshu.contracts.research import ResearchTrigger, StrategyPackage


def test_backtest_report_requires_timezone_aware_generated_at() -> None:
    with pytest.raises(ValidationError):
        BacktestReport(
            backtest_report_id="bt-1",
            strategy_package_id="pkg-1",
            symbol_scope=["BTC-USDT-SWAP"],
            dataset_range={"start": "2026-04-01T00:00:00Z", "end": "2026-04-02T00:00:00Z"},
            sample_count=10,
            trade_count=4,
            net_pnl=12.5,
            max_drawdown=3.2,
            win_rate=0.5,
            profit_factor=1.3,
            stability_score=0.7,
            overfit_risk="low",
            failure_modes=["late breakouts"],
            invalidating_conditions=["spread expansion"],
            generated_at=datetime(2026, 4, 20, 12, 0, 0),
        )


def test_approval_record_accepts_approved_with_guardrails() -> None:
    record = ApprovalRecord(
        approval_record_id="apr-1",
        strategy_package_id="pkg-1",
        backtest_report_id="bt-1",
        decision=ApprovalDecision.APPROVED_WITH_GUARDRAILS,
        decision_reason="usable with reduced scope",
        guardrails={"market_mode": "degraded"},
        reviewed_by="committee",
        review_source="telegram",
        created_at=datetime.now(UTC),
    )

    assert record.decision == ApprovalDecision.APPROVED_WITH_GUARDRAILS


def test_approval_record_requires_timezone_aware_created_at() -> None:
    with pytest.raises(ValidationError):
        ApprovalRecord(
            approval_record_id="apr-2",
            strategy_package_id="pkg-1",
            backtest_report_id="bt-1",
            decision=ApprovalDecision.REJECTED,
            decision_reason="insufficient evidence",
            guardrails={},
            reviewed_by="committee",
            review_source="manual",
            created_at=datetime(2026, 4, 20, 12, 0, 0),
        )


def test_backtest_report_normalizes_timezone_aware_generated_at_to_utc() -> None:
    report = BacktestReport(
        backtest_report_id="bt-2",
        strategy_package_id="pkg-1",
        symbol_scope=["BTC-USDT-SWAP"],
        dataset_range={"start": "2026-04-01T00:00:00Z", "end": "2026-04-02T00:00:00Z"},
        sample_count=10,
        trade_count=4,
        net_pnl=12.5,
        max_drawdown=3.2,
        win_rate=0.5,
        profit_factor=1.3,
        stability_score=0.7,
        overfit_risk="low",
        failure_modes=["late breakouts"],
        invalidating_conditions=["spread expansion"],
        generated_at=datetime(2026, 4, 20, 20, 0, 0, tzinfo=timezone(timedelta(hours=8))),
    )

    assert report.generated_at == datetime(2026, 4, 20, 12, 0, tzinfo=UTC)


def test_approval_record_normalizes_timezone_aware_created_at_to_utc() -> None:
    record = ApprovalRecord(
        approval_record_id="apr-3",
        strategy_package_id="pkg-1",
        backtest_report_id="bt-1",
        decision=ApprovalDecision.APPROVED,
        decision_reason="usable",
        guardrails={},
        reviewed_by="committee",
        review_source="manual",
        created_at=datetime(2026, 4, 20, 20, 0, 0, tzinfo=timezone(timedelta(hours=8))),
    )

    assert record.created_at == datetime(2026, 4, 20, 12, 0, tzinfo=UTC)


def test_strategy_package_requires_timezone_aware_generated_at() -> None:
    with pytest.raises(ValidationError):
        StrategyPackage(
            strategy_package_id="pkg-1",
            generated_at=datetime(2026, 4, 20, 12, 0, 0),
            trigger=ResearchTrigger.MANUAL,
            symbol_scope=["BTC-USDT-SWAP"],
            market_environment_scope=["trend"],
            strategy_family="breakout",
            directionality="long",
            entry_rules={},
            exit_rules={},
            position_sizing_rules={},
            risk_constraints={},
            parameter_set={},
            backtest_summary={},
            performance_summary={},
            failure_modes=["late breakouts"],
            invalidating_conditions=["spread expansion"],
            research_reason="manual study",
        )


def test_strategy_package_normalizes_timezone_aware_generated_at_to_utc() -> None:
    package = StrategyPackage(
        strategy_package_id="pkg-1",
        generated_at=datetime(2026, 4, 20, 20, 0, 0, tzinfo=timezone(timedelta(hours=8))),
        trigger=ResearchTrigger.MANUAL,
        symbol_scope=["BTC-USDT-SWAP"],
        market_environment_scope=["trend"],
        strategy_family="breakout",
        directionality="long",
        entry_rules={},
        exit_rules={},
        position_sizing_rules={},
        risk_constraints={},
        parameter_set={},
        backtest_summary={},
        performance_summary={},
        failure_modes=["late breakouts"],
        invalidating_conditions=["spread expansion"],
        research_reason="manual study",
    )

    assert package.generated_at == datetime(2026, 4, 20, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "field_name",
    ["net_pnl", "max_drawdown", "win_rate", "profit_factor", "stability_score"],
)
@pytest.mark.parametrize("bad_value", [math.inf, -math.inf, math.nan])
def test_backtest_report_rejects_non_finite_float_metrics(field_name: str, bad_value: float) -> None:
    payload = {
        "backtest_report_id": "bt-3",
        "strategy_package_id": "pkg-1",
        "symbol_scope": ["BTC-USDT-SWAP"],
        "dataset_range": {"start": "2026-04-01T00:00:00Z", "end": "2026-04-02T00:00:00Z"},
        "sample_count": 10,
        "trade_count": 4,
        "net_pnl": 12.5,
        "max_drawdown": 3.2,
        "win_rate": 0.5,
        "profit_factor": 1.3,
        "stability_score": 0.7,
        "overfit_risk": "low",
        "failure_modes": ["late breakouts"],
        "invalidating_conditions": ["spread expansion"],
        "generated_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
    }
    payload[field_name] = bad_value

    with pytest.raises(ValidationError):
        BacktestReport(**payload)

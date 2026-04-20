from datetime import UTC, datetime, timedelta, timezone
import math
from decimal import Decimal

import pytest
from pydantic import ValidationError

from xuanshu.contracts.approval import ApprovalDecision, ApprovalRecord
from xuanshu.contracts.backtest import (
    BacktestDatasetRange,
    BacktestReport,
    OverfitRisk,
    RegimeFit,
    TradeCountSufficiency,
)
from xuanshu.contracts.research import ResearchTrigger, StrategyPackage
from xuanshu.governor.backtest import BacktestValidator


def test_backtest_report_requires_timezone_aware_generated_at() -> None:
    with pytest.raises(ValidationError):
        BacktestReport(
            backtest_report_id="bt-1",
            strategy_package_id="pkg-1",
            symbol_scope=["BTC-USDT-SWAP"],
            dataset_range=BacktestDatasetRange(
                start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
                end=datetime(2026, 4, 2, 0, 0, tzinfo=UTC),
                regime_fit=RegimeFit.ALIGNED,
            ),
            sample_count=10,
            trade_count=4,
            trade_count_sufficiency=TradeCountSufficiency.SUFFICIENT,
            net_pnl=12.5,
            max_drawdown=3.2,
            win_rate=0.5,
            profit_factor=1.3,
            stability_score=0.7,
            overfit_risk=OverfitRisk.LOW,
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
        dataset_range=BacktestDatasetRange(
            start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 4, 2, 0, 0, tzinfo=UTC),
            regime_fit=RegimeFit.ALIGNED,
        ),
        sample_count=10,
        trade_count=4,
        trade_count_sufficiency=TradeCountSufficiency.SUFFICIENT,
        net_pnl=12.5,
        max_drawdown=3.2,
        win_rate=0.5,
        profit_factor=1.3,
        stability_score=0.7,
        overfit_risk=OverfitRisk.LOW,
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
        "dataset_range": BacktestDatasetRange(
            start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 4, 2, 0, 0, tzinfo=UTC),
            regime_fit=RegimeFit.ALIGNED,
        ),
        "sample_count": 10,
        "trade_count": 4,
        "trade_count_sufficiency": TradeCountSufficiency.SUFFICIENT,
        "net_pnl": 12.5,
        "max_drawdown": 3.2,
        "win_rate": 0.5,
        "profit_factor": 1.3,
        "stability_score": 0.7,
        "overfit_risk": OverfitRisk.LOW,
        "failure_modes": ["late breakouts"],
        "invalidating_conditions": ["spread expansion"],
        "generated_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
    }
    payload[field_name] = bad_value

    with pytest.raises(ValidationError):
        BacktestReport(**payload)


def test_backtest_report_rejects_invalid_dataset_range_shape() -> None:
    with pytest.raises(ValidationError):
        BacktestReport(
            backtest_report_id="bt-4",
            strategy_package_id="pkg-1",
            symbol_scope=["BTC-USDT-SWAP"],
            dataset_range={
                "start": datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
                "end": datetime(2026, 4, 2, 0, 0, tzinfo=UTC),
                "regime_fit": "aligned",
                "extra": "unexpected",
            },
            sample_count=10,
            trade_count=4,
            trade_count_sufficiency=TradeCountSufficiency.SUFFICIENT,
            net_pnl=12.5,
            max_drawdown=3.2,
            win_rate=0.5,
            profit_factor=1.3,
            stability_score=0.7,
            overfit_risk=OverfitRisk.LOW,
            failure_modes=["late breakouts"],
            invalidating_conditions=["spread expansion"],
            generated_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
        )


def test_backtest_dataset_range_rejects_reversed_range() -> None:
    with pytest.raises(ValidationError):
        BacktestDatasetRange(
            start=datetime(2026, 4, 2, 0, 0, tzinfo=UTC),
            end=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            regime_fit=RegimeFit.ALIGNED,
        )


def _make_package(
    *,
    strategy_family: str = "breakout",
    directionality: str = "long_only",
    signal: str | None = None,
    symbol_scope: list[str] | None = None,
    market_environment_scope: list[str] | None = None,
    lookback: int = 1,
    stop_loss_bps: int = 50,
    take_profit_bps: int = 100,
    risk_fraction: float = 1.0,
    max_hold_minutes: int = 60,
) -> StrategyPackage:
    if signal is None:
        signal = "breakout_confirmed" if strategy_family == "breakout" else "mean_reversion_signal"
    if symbol_scope is None:
        symbol_scope = ["BTC-USDT-SWAP"]
    if market_environment_scope is None:
        market_environment_scope = ["trend"] if strategy_family == "breakout" else ["range"]
    return StrategyPackage(
        strategy_package_id="pkg-1",
        generated_at=datetime.now(UTC),
        trigger=ResearchTrigger.SCHEDULE,
        symbol_scope=symbol_scope,
        market_environment_scope=market_environment_scope,
        strategy_family=strategy_family,
        directionality=directionality,
        entry_rules={"signal": signal},
        exit_rules={"stop_loss_bps": stop_loss_bps, "take_profit_bps": take_profit_bps},
        position_sizing_rules={"risk_fraction": risk_fraction},
        risk_constraints={"max_hold_minutes": max_hold_minutes},
        parameter_set={"lookback": lookback},
        backtest_summary={},
        performance_summary={},
        failure_modes=["late"],
        invalidating_conditions=["spread expansion"],
        research_reason="scheduled research",
    )


def _build_rows(closes: list[object]) -> list[dict[str, object]]:
    start = datetime(2026, 4, 19, 0, 0, tzinfo=UTC)
    return [
        {"timestamp": start + timedelta(minutes=index), "close": close}
        for index, close in enumerate(closes)
    ]


def test_backtest_validator_builds_strategy_aware_report() -> None:
    report = BacktestValidator().validate(
        package=_make_package(take_profit_bps=80),
        historical_rows=_build_rows([100.0, 101.0, 102.0, 103.0, 104.0]),
    )

    assert report.backtest_report_id.startswith("pkg-1-report-")
    assert report.strategy_package_id == "pkg-1"
    assert report.sample_count == 5
    assert report.trade_count == 2
    assert report.trade_count_sufficiency == TradeCountSufficiency.SUFFICIENT
    assert report.net_pnl == pytest.approx(0.01960972796308757)
    assert report.win_rate == 1.0
    assert report.profit_factor == 999.0
    assert report.generated_at == datetime(2026, 4, 19, 0, 4, tzinfo=UTC)
    assert report.dataset_range.regime_fit == RegimeFit.ALIGNED
    assert report.overfit_risk == OverfitRisk.MEDIUM


def test_backtest_validator_normalizes_row_order_before_simulation() -> None:
    rows = [
        {"timestamp": datetime(2026, 4, 19, 0, 4, tzinfo=UTC), "close": 104.0},
        {"timestamp": datetime(2026, 4, 19, 0, 3, tzinfo=UTC), "close": 103.0},
        {"timestamp": datetime(2026, 4, 19, 0, 1, tzinfo=UTC), "close": 101.0},
        {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 100.0},
        {"timestamp": datetime(2026, 4, 19, 0, 2, tzinfo=UTC), "close": 102.0},
    ]

    report = BacktestValidator().validate(package=_make_package(take_profit_bps=80), historical_rows=rows)

    assert report.dataset_range == BacktestDatasetRange(
        start=datetime(2026, 4, 19, 0, 0, tzinfo=UTC),
        end=datetime(2026, 4, 19, 0, 4, tzinfo=UTC),
        regime_fit=RegimeFit.ALIGNED,
    )
    assert report.trade_count == 2
    assert report.net_pnl == pytest.approx(0.01960972796308757)


def test_backtest_validator_does_not_reenter_on_the_same_bar_as_an_exit() -> None:
    report = BacktestValidator().validate(
        package=_make_package(take_profit_bps=80),
        historical_rows=_build_rows([100.0, 101.0, 102.0, 103.0]),
    )

    assert report.trade_count == 1
    assert report.trade_count_sufficiency == TradeCountSufficiency.INSUFFICIENT
    assert report.net_pnl == pytest.approx((102.0 - 101.0) / 101.0)


def test_strategy_family_changes_behavior_on_same_rows() -> None:
    rows = _build_rows([100.0, 101.0, 99.0])

    breakout_report = BacktestValidator().validate(
        package=_make_package(strategy_family="breakout", directionality="long_short"),
        historical_rows=rows,
    )
    mean_reversion_report = BacktestValidator().validate(
        package=_make_package(
            strategy_family="mean_reversion",
            directionality="long_short",
            signal="mean_reversion_signal",
        ),
        historical_rows=rows,
    )

    assert breakout_report.net_pnl < 0
    assert mean_reversion_report.net_pnl > 0
    assert breakout_report.trade_count == mean_reversion_report.trade_count == 1


def test_directionality_blocks_disallowed_trade_direction() -> None:
    rows = _build_rows([100.0, 99.0, 98.0])

    blocked_report = BacktestValidator().validate(
        package=_make_package(directionality="long_only"),
        historical_rows=rows,
    )
    allowed_report = BacktestValidator().validate(
        package=_make_package(directionality="long_short"),
        historical_rows=rows,
    )

    assert blocked_report.trade_count == 0
    assert blocked_report.net_pnl == 0.0
    assert allowed_report.trade_count == 1
    assert allowed_report.net_pnl > 0


def test_lookback_affects_entries() -> None:
    rows = _build_rows([100.0, 101.0, 100.4])

    short_lookback_report = BacktestValidator().validate(
        package=_make_package(lookback=1),
        historical_rows=rows,
    )
    long_lookback_report = BacktestValidator().validate(
        package=_make_package(lookback=2),
        historical_rows=rows,
    )

    assert short_lookback_report.trade_count == 1
    assert long_lookback_report.trade_count == 0
    assert short_lookback_report.net_pnl != long_lookback_report.net_pnl


def test_take_profit_exit_path_is_exercised() -> None:
    report = BacktestValidator().validate(
        package=_make_package(take_profit_bps=80, stop_loss_bps=200),
        historical_rows=_build_rows([100.0, 101.0, 102.0]),
    )

    assert report.trade_count == 1
    assert report.net_pnl == pytest.approx((102.0 - 101.0) / 101.0)


def test_stop_loss_exit_path_is_exercised() -> None:
    report = BacktestValidator().validate(
        package=_make_package(stop_loss_bps=50, take_profit_bps=500),
        historical_rows=_build_rows([100.0, 101.0, 100.0]),
    )

    assert report.trade_count == 1
    assert report.net_pnl == pytest.approx((100.0 - 101.0) / 101.0)
    assert report.win_rate == 0.0


def test_max_hold_exit_path_is_exercised() -> None:
    report = BacktestValidator().validate(
        package=_make_package(stop_loss_bps=500, take_profit_bps=500, max_hold_minutes=1),
        historical_rows=_build_rows([100.0, 101.0, 101.1]),
    )

    assert report.trade_count == 1
    assert report.net_pnl == pytest.approx((101.1 - 101.0) / 101.0)


def test_realized_trade_metrics_do_not_follow_raw_final_net_move() -> None:
    report = BacktestValidator().validate(
        package=_make_package(directionality="long_short", stop_loss_bps=50, take_profit_bps=500),
        historical_rows=_build_rows([100.0, 103.0, 100.0, 105.0, 104.0]),
    )

    assert report.trade_count == 2
    assert report.net_pnl < 0
    assert report.win_rate == 0.0


def test_trade_count_sufficiency_reflects_realized_trade_boundary() -> None:
    insufficient_report = BacktestValidator().validate(
        package=_make_package(take_profit_bps=80),
        historical_rows=_build_rows([100.0, 101.0, 102.0]),
    )
    sufficient_report = BacktestValidator().validate(
        package=_make_package(take_profit_bps=80),
        historical_rows=_build_rows([100.0, 101.0, 102.0, 103.0, 104.0]),
    )

    assert insufficient_report.trade_count == 1
    assert insufficient_report.trade_count_sufficiency == TradeCountSufficiency.INSUFFICIENT
    assert sufficient_report.trade_count == 2
    assert sufficient_report.trade_count_sufficiency == TradeCountSufficiency.SUFFICIENT


def test_backtest_validator_reports_unknown_regime_fit_for_non_matching_scope() -> None:
    report = BacktestValidator().validate(
        package=_make_package(
            strategy_family="mean_reversion",
            signal="range_retest",
            market_environment_scope=["trend"],
            lookback=2,
        ),
        historical_rows=_build_rows([100.0, 99.0, 100.0]),
    )

    assert report.dataset_range.regime_fit == RegimeFit.UNKNOWN


def test_backtest_validator_treats_market_environment_scope_as_membership() -> None:
    report = BacktestValidator().validate(
        package=_make_package(market_environment_scope=["range", "trend"]),
        historical_rows=_build_rows([100.0, 101.0, 102.0, 103.0]),
    )

    assert report.dataset_range.regime_fit == RegimeFit.ALIGNED


def test_backtest_validator_rejects_duplicate_timestamps() -> None:
    rows = _build_rows([100.0, 101.0])
    rows[1]["timestamp"] = rows[0]["timestamp"]

    with pytest.raises(ValueError, match="historical_rows timestamps must be unique"):
        BacktestValidator().validate(package=_make_package(), historical_rows=rows)


def test_backtest_validator_rejects_multi_symbol_packages() -> None:
    with pytest.raises(ValueError, match="BacktestValidator currently supports exactly one symbol"):
        BacktestValidator().validate(
            package=_make_package(symbol_scope=["BTC-USDT-SWAP", "ETH-USDT-SWAP"]),
            historical_rows=_build_rows([100.0, 101.0]),
        )


@pytest.mark.parametrize(
    ("close_value", "error_message"),
    [
        (True, "historical_rows close values must be real numbers"),
        ("101.0", "historical_rows close values must be real numbers"),
        (0.0, "historical_rows close values must be > 0"),
        (-1.0, "historical_rows close values must be > 0"),
        (math.inf, "historical_rows close values must be finite"),
        (math.nan, "historical_rows close values must be finite"),
    ],
)
def test_backtest_validator_rejects_malformed_close_values(
    close_value: object,
    error_message: str,
) -> None:
    rows = _build_rows([100.0, 101.0])
    rows[1]["close"] = close_value

    with pytest.raises(ValueError, match=error_message):
        BacktestValidator().validate(package=_make_package(), historical_rows=rows)


def test_backtest_validator_accepts_decimal_close_values() -> None:
    report = BacktestValidator().validate(
        package=_make_package(),
        historical_rows=_build_rows([Decimal("100.0"), Decimal("101.0"), Decimal("102.0")]),
    )

    assert report.net_pnl > 0


def test_mean_reversion_signals_produce_distinct_behavior_on_same_rows() -> None:
    rows = _build_rows([100.0, 98.0, 97.0, 99.0, 101.0])

    immediate_signal_report = BacktestValidator().validate(
        package=_make_package(
            strategy_family="mean_reversion",
            signal="mean_reversion_signal",
            directionality="long_only",
            lookback=2,
            take_profit_bps=100,
            stop_loss_bps=500,
        ),
        historical_rows=rows,
    )
    retest_signal_report = BacktestValidator().validate(
        package=_make_package(
            strategy_family="mean_reversion",
            signal="range_retest",
            directionality="long_only",
            lookback=2,
            take_profit_bps=100,
            stop_loss_bps=500,
        ),
        historical_rows=rows,
    )

    assert immediate_signal_report.trade_count == 1
    assert retest_signal_report.trade_count == 1
    assert immediate_signal_report.net_pnl != retest_signal_report.net_pnl


def test_range_retest_requires_touching_the_lookback_anchor() -> None:
    rows = _build_rows([100.0, 98.0, 97.0, 97.5, 99.0])

    report = BacktestValidator().validate(
        package=_make_package(
            strategy_family="mean_reversion",
            signal="range_retest",
            directionality="long_only",
            lookback=2,
            take_profit_bps=100,
            stop_loss_bps=500,
        ),
        historical_rows=rows,
    )

    assert report.trade_count == 0
    assert report.net_pnl == 0.0


def test_range_retest_accepts_exact_touch_of_the_lookback_anchor() -> None:
    report = BacktestValidator().validate(
        package=_make_package(
            strategy_family="mean_reversion",
            signal="range_retest",
            directionality="long_only",
            lookback=2,
            take_profit_bps=100,
            stop_loss_bps=500,
        ),
        historical_rows=_build_rows([100.0, 98.0, 97.0, 98.0, 99.0]),
    )

    assert report.trade_count == 1
    assert report.net_pnl > 0


def test_range_retest_accepts_exact_touch_of_the_lookback_anchor_for_short_side() -> None:
    report = BacktestValidator().validate(
        package=_make_package(
            strategy_family="mean_reversion",
            signal="range_retest",
            directionality="short_only",
            lookback=2,
            take_profit_bps=100,
            stop_loss_bps=500,
        ),
        historical_rows=_build_rows([100.0, 102.0, 103.0, 102.0, 101.0]),
    )

    assert report.trade_count == 1
    assert report.net_pnl > 0


def test_backtest_validator_rejects_range_retest_with_lookback_one() -> None:
    with pytest.raises(ValueError, match="range_retest requires lookback >= 2"):
        BacktestValidator().validate(
            package=_make_package(
                strategy_family="mean_reversion",
                signal="range_retest",
                lookback=1,
            ),
            historical_rows=_build_rows([100.0, 99.0, 100.0]),
        )


def test_backtest_validator_uses_unit_profit_factor_for_zero_loss_series() -> None:
    report = BacktestValidator().validate(
        package=_make_package(),
        historical_rows=_build_rows([100.0, 101.0, 102.0, 103.0]),
    )

    assert report.profit_factor == 999.0


def test_backtest_validator_uses_distinct_report_ids_for_different_dataset_windows() -> None:
    early_report = BacktestValidator().validate(
        package=_make_package(),
        historical_rows=_build_rows([100.0, 101.0, 102.0]),
    )
    later_report = BacktestValidator().validate(
        package=_make_package(),
        historical_rows=_build_rows([101.0, 102.0, 103.0]),
    )

    assert early_report.backtest_report_id != later_report.backtest_report_id


def test_backtest_validator_report_id_changes_with_schema_version() -> None:
    original_version = BacktestValidator.REPORT_SCHEMA_VERSION
    rows = _build_rows([100.0, 101.0, 102.0])
    try:
        BacktestValidator.REPORT_SCHEMA_VERSION = "v1"
        v1_report = BacktestValidator().validate(package=_make_package(), historical_rows=rows)
        BacktestValidator.REPORT_SCHEMA_VERSION = "v2"
        v2_report = BacktestValidator().validate(package=_make_package(), historical_rows=rows)
    finally:
        BacktestValidator.REPORT_SCHEMA_VERSION = original_version

    assert v1_report.backtest_report_id != v2_report.backtest_report_id


def test_backtest_validator_uses_distinct_report_ids_for_precision_distinct_rows() -> None:
    precise_close = math.nextafter(100.0, math.inf)
    base_report = BacktestValidator().validate(
        package=_make_package(),
        historical_rows=_build_rows([100.0, 101.0]),
    )
    precision_report = BacktestValidator().validate(
        package=_make_package(),
        historical_rows=_build_rows([precise_close, 101.0]),
    )

    assert base_report.backtest_report_id != precision_report.backtest_report_id


def test_backtest_validator_rejects_invalid_package_parameters() -> None:
    with pytest.raises(ValueError, match="lookback must be >= 1"):
        BacktestValidator().validate(
            package=_make_package(lookback=0),
            historical_rows=_build_rows([100.0, 101.0]),
        )

    with pytest.raises(ValueError, match="risk_fraction must be > 0"):
        BacktestValidator().validate(
            package=_make_package(risk_fraction=0.0),
            historical_rows=_build_rows([100.0, 101.0]),
        )

    with pytest.raises(ValueError, match="risk_fraction must be <= 1"):
        BacktestValidator().validate(
            package=_make_package(risk_fraction=1.5),
            historical_rows=_build_rows([100.0, 101.0]),
        )


@pytest.mark.parametrize("bad_timestamp", ["2026-04-19T00:00:00Z", 1713484800, None])
def test_backtest_validator_rejects_malformed_timestamps(bad_timestamp: object) -> None:
    rows = _build_rows([100.0, 101.0])
    rows[1]["timestamp"] = bad_timestamp

    with pytest.raises(ValueError, match="historical_rows timestamp values must be datetimes"):
        BacktestValidator().validate(package=_make_package(), historical_rows=rows)

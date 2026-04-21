# Governor Research / Backtest / Approval Loop Design

## 1. Scope

This spec defines the missing governance loop required by the existing V1 architecture:

`Strategy Research -> backtest/validation -> Decision Committee -> Snapshot Publisher -> StrategyConfigSnapshot`

This spec is intentionally limited to the slow-path governance chain. It does not restore live trading, does not restart the trader, and does not change the current manual takeover of the real position. `Trader Service` remains stopped until this loop is implemented, verified, and explicitly released later.

## 2. Problem Statement

The current production implementation does not satisfy the documented architecture:

- `Governor` has only a lightweight research stub
- there is no real historical backtest / validation stage
- there is no durable approval object or approval workflow
- approved research does not exist as a first-class audited artifact
- snapshot publication is not gated by a real approval chain

As a result:

- `research_status` frequently remains `skipped`
- candidate count stays zero or is not durable
- governance output cannot be audited as a proper research-to-approval pipeline
- `Trader` cannot safely consume governance output as the sole source of truth

## 3. Goals

The implementation produced from this spec must satisfy these goals:

- run scheduled research jobs inside `Governor`
- build candidate strategy packages from historical real data
- run deterministic historical backtest / validation on those candidates
- persist candidate, validation, and approval artifacts
- require explicit committee approval before any research result can influence snapshots
- publish `StrategyConfigSnapshot` only from approved research outcomes
- expose approval and inspection via the existing `Notifier / Telegram` control surface
- keep the entire loop auditable through PostgreSQL facts plus Redis hot summaries

## 4. Non-Goals

This spec does not:

- re-enable real trading
- switch the trader back on
- fix the trader live execution path in this phase
- implement a fully general quantitative research platform
- add multi-exchange, multi-asset, or distributed scheduling

## 5. Architecture

The architecture remains aligned with the existing V1 documents:

- `Strategy Research` stays inside `Governor`, not a separate service
- `Decision Committee` remains the only approval authority
- `Snapshot Publisher` remains the only path that can change trader-consumable strategy configuration
- `Notifier` remains the human interaction surface, not the source of truth

The effective loop becomes:

1. `Governor` trigger fires
2. `Strategy Research` builds one or more `StrategyPackage` candidates
3. `Backtest / Validation` runs deterministic validation over historical facts
4. `Decision Committee` evaluates expert opinions plus validation results
5. approval result is persisted as an auditable `ApprovalRecord`
6. `Snapshot Publisher` publishes a new snapshot only when approval result allows it
7. `Notifier` exposes status, pending approvals, and approve / reject actions

## 6. Runtime Triggers

The implementation must support the three trigger classes already documented:

- scheduled trigger
- event-driven trigger
- manual trigger

Priority remains:

1. manual
2. event
3. schedule

Only a bounded number of research jobs may run concurrently. A trigger that arrives while an equivalent job is already active should be coalesced, not duplicated.

## 7. Data Contracts

### 7.1 StrategyPackage

`StrategyPackage` remains the research output, but it must become durable and complete enough for committee review.

Each package must include at least:

- `strategy_package_id`
- `trigger_type`
- `symbol_scope`
- `market_environment_scope`
- `strategy_family`
- `directionality`
- `entry_rules`
- `exit_rules`
- `position_sizing_rules`
- `risk_constraints`
- `parameter_set`
- `research_reason`
- `generated_at`

### 7.2 BacktestReport

Add a first-class `BacktestReport` contract that captures validation output for one `StrategyPackage`.

Minimum fields:

- `backtest_report_id`
- `strategy_package_id`
- `symbol_scope`
- `dataset_range`
- `sample_count`
- `trade_count`
- `net_pnl`
- `max_drawdown`
- `win_rate`
- `profit_factor`
- `stability_score`
- `overfit_risk`
- `failure_modes`
- `invalidating_conditions`
- `generated_at`

### 7.3 ApprovalRecord

Add a first-class `ApprovalRecord` contract that represents committee output.

Minimum fields:

- `approval_record_id`
- `strategy_package_id`
- `backtest_report_id`
- `decision`
- `decision_reason`
- `guardrails`
- `reviewed_by`
- `review_source`
- `created_at`

Allowed decisions:

- `approved`
- `approved_with_guardrails`
- `rejected`
- `needs_revision`

## 8. Persistence Model

The loop must persist three new fact streams into PostgreSQL:

- `strategy_packages`
- `backtest_reports`
- `approval_records`

Additionally:

- `governor_runs` must record trigger type, research status, validation status, and approval status
- `strategy_snapshots` must record the source package and source approval record used to publish the snapshot

Redis remains hot-state only and should expose:

- latest pending approval summary
- latest approved package summary
- latest backtest health summary
- current governor health summary

## 9. Historical Data Inputs

The research and validation chain must use real persisted historical facts rather than synthetic placeholders.

Initial input sources:

- `orders`
- `fills`
- `positions`
- `risk_events`
- latest and recent `strategy_snapshots`

If there is insufficient data, the system must:

- mark research as `insufficient_history`
- persist that outcome
- avoid publishing any new snapshot from that run

This must be explicit, not silently collapsed into generic `skipped`.

## 10. Validation Rules

Backtest / validation must be deterministic and system-owned.

AI may:

- suggest hypotheses
- suggest parameter search spaces
- help interpret results

AI may not:

- replace the backtest engine
- declare a package approved
- bypass validation
- directly publish a snapshot

The validator must score candidates on more than raw PnL. At minimum it must incorporate:

- profitability
- drawdown
- stability
- trade count sufficiency
- overfit risk
- regime fit

## 11. Approval Flow

Approval truth must live in system state, not inside Telegram messages.

The committee flow is:

1. research package persisted
2. backtest report persisted
3. committee evaluates package + report + expert opinions
4. committee emits `ApprovalRecord`
5. snapshot publisher checks `ApprovalRecord`
6. only `approved` or `approved_with_guardrails` may publish

`approved_with_guardrails` must support at least:

- reduced symbol scope
- reduced risk multiplier
- restricted environment scope
- forced `degraded` market mode

`rejected` and `needs_revision` must block publication.

## 12. Notifier Integration

`Notifier` is the operator-facing interaction surface.

It must add commands for:

- listing pending research approvals
- showing a package summary
- showing a backtest summary
- approving a package
- rejecting a package

The command surface must only mutate durable approval state. It must not directly edit snapshots.

## 13. Snapshot Publication Rules

`Snapshot Publisher` must enforce:

- no approval record => no publish
- rejected / needs_revision => no publish
- approved_with_guardrails => publish only with guardrail adjustments embedded
- approved => publish standard snapshot

Each published snapshot must reference:

- source `strategy_package_id`
- source `backtest_report_id`
- source `approval_record_id`

## 14. Failure Handling

Failure rules:

- research provider failure => persist failure, no publish
- validation failure => persist failure, no publish
- approval missing => persist pending state, no publish
- snapshot publication failure => keep previous snapshot, persist failure
- notifier failure => does not block governor loop or approval truth persistence

## 15. Acceptance Criteria

This sub-project is complete only when all of the following are true:

1. Scheduled governor cycles can produce durable `StrategyPackage` candidates from historical facts.
2. Each candidate can run through a deterministic `BacktestReport` generation step.
3. Research outcomes no longer collapse into opaque `skipped`; they resolve into explicit statuses such as `succeeded`, `failed`, or `insufficient_history`.
4. Committee approval produces durable `ApprovalRecord` rows.
5. Unapproved candidates never influence `StrategyConfigSnapshot`.
6. Approved candidates can publish a new snapshot through `Snapshot Publisher`.
7. Published snapshots carry source references back to package, validation, and approval artifacts.
8. `Notifier` exposes pending approvals and approve / reject actions without becoming the approval source of truth.
9. The entire loop runs with `Trader` stopped and does not require live execution to validate correctness.

## 16. Implementation Order

Implementation should proceed in this order:

1. contracts for package / report / approval
2. persistence boundaries for new fact types
3. failing tests for publish gating and explicit research statuses
4. minimal deterministic backtest / validation engine
5. governor runtime wiring for research -> validation -> approval -> publish
6. notifier approval commands
7. end-to-end integration tests for the governance loop

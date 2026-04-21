# AI Strategy Generation Design

> Status: Draft approved in conversation on 2026-04-21. This document captures the agreed design before implementation planning.

## Goal

Extend `xuanshu` from a system that mainly toggles and tunes predefined strategy behaviors into a system that can:

- let AI generate new candidate strategies and parameter sets
- backtest those candidates automatically
- keep only candidates whose backtest interval return is greater than `50%`
- submit surviving candidates into the existing approval flow
- enforce that each trading symbol runs only one active strategy at a time
- allow a stronger approved strategy to replace the active strategy only if its backtest interval return is at least `10%` higher than the current active strategy's score

The system must not execute arbitrary AI-generated Python code. All generated strategies must be represented in a constrained, auditable strategy DSL that can be interpreted by both backtesting and trading runtimes.

## Current Context

The current codebase already has a useful governance and execution skeleton:

- `Governor` can build research candidates, backtest them, write audit rows, generate strategy snapshots, and manage approvals.
- `Trader` can consume a strategy snapshot, evaluate risk, and execute orders against OKX.
- `Notifier`, Redis, PostgreSQL, and Qdrant provide operational plumbing, state sharing, and audit surfaces.

What is missing is a unified strategy representation that both the research path and execution path can understand. Today:

- candidate research packages are richer than execution snapshots
- execution logic is still tied to a small fixed set of strategy behaviors
- strategy discovery is not yet a true search over executable strategy definitions

This design closes that gap by introducing a constrained strategy DSL and making it the canonical format across research, backtest, approval, snapshot publication, and live execution.

## Requirements

### Functional Requirements

1. AI can generate entirely new strategy candidates, not just parameter tweaks for `breakout` and `mean_reversion`.
2. Each candidate must be serializable as a constrained strategy DSL / IR, not source code.
3. The governor research path must generate multiple candidate strategies and parameter variants per symbol.
4. Every candidate must be backtested through the same execution semantics used by the live trader as closely as practical in this phase.
5. A candidate is retained only when `backtest_return_percent > 50`.
6. Retained candidates must be submitted to the existing approval flow through `strategy_packages`, `backtest_reports`, and `approval_records`.
7. At runtime, each symbol may have only one active strategy owner at a time.
8. If a newly approved strategy for a symbol is stronger than the active strategy by at least `10%`, the trader must switch by canceling old orders, flattening the old position, and then allowing the new strategy to open.
9. If the new strategy does not exceed the current active strategy score by at least `10%`, no replacement occurs.

### Explicit Non-Goals

- No arbitrary Python code generation or dynamic code loading.
- No multi-strategy concurrent execution on the same symbol.
- No optimization target beyond backtest interval total return in phase 1.
- No additional hard filters for drawdown, stability, overfit risk, or trade count in phase 1.
- No portfolio-level strategy allocator in phase 1.

## Strategy DSL

### Design Principles

The DSL must be:

- constrained enough to validate and audit safely
- expressive enough for AI to generate genuinely new strategies
- deterministic to interpret in both backtest and live execution
- backward-compatible enough to map existing simple strategies into the new structure

### Canonical Structure

Each executable strategy definition should contain:

- `strategy_def_id`
- `symbol`
- `strategy_family`
- `directionality`
- `feature_spec`
- `entry_rules`
- `exit_rules`
- `position_sizing_rules`
- `risk_constraints`
- `parameter_set`
- `score`
- `score_basis`

`strategy_family` remains a label for grouping and observability, not a hard-coded execution path selector. The actual behavior comes from the DSL fields.

### DSL Shape

The first phase should support a restricted expression tree rather than a free-form expression language. A candidate structure:

```json
{
  "strategy_def_id": "strat-btc-001",
  "symbol": "BTC-USDT-SWAP",
  "strategy_family": "volatility_break_retest",
  "directionality": "long_only",
  "feature_spec": {
    "indicators": [
      {"name": "sma", "source": "close", "window": 20},
      {"name": "sma", "source": "close", "window": 50},
      {"name": "atr", "window": 14},
      {"name": "zscore", "source": "close", "window": 30}
    ]
  },
  "entry_rules": {
    "all": [
      {"op": "crosses_above", "left": "close", "right": "sma_20"},
      {"op": "greater_than", "left": "sma_20", "right": "sma_50"},
      {"op": "less_than", "left": "atr_14", "right": {"const": 500}}
    ]
  },
  "exit_rules": {
    "any": [
      {"op": "crosses_below", "left": "close", "right": "sma_20"},
      {"op": "take_profit_bps", "value": 900},
      {"op": "stop_loss_bps", "value": 300}
    ]
  },
  "position_sizing_rules": {
    "risk_fraction": 0.01
  },
  "risk_constraints": {
    "max_hold_minutes": 240
  },
  "parameter_set": {
    "fast_window": 20,
    "slow_window": 50,
    "atr_cap": 500
  },
  "score": 67.5,
  "score_basis": "backtest_return_percent"
}
```

### Phase 1 Supported Building Blocks

To keep the scope bounded, phase 1 should support a fixed operator set:

- indicator primitives: `sma`, `ema`, `atr`, `highest`, `lowest`, `zscore`
- value sources: `open`, `high`, `low`, `close`, `volume`
- comparison operators: `greater_than`, `less_than`, `crosses_above`, `crosses_below`
- boolean combinators: `all`, `any`
- exit primitives: `take_profit_bps`, `stop_loss_bps`, `time_stop_minutes`
- directionality: `long_only`, `short_only`

This is enough to allow AI to synthesize new strategy shapes without opening an unbounded execution model.

## Research Generation Flow

### Generator Responsibilities

The AI strategy generator is responsible for:

- reading symbol scope, market environment, and historical summary
- proposing new DSL strategy definitions
- proposing initial parameter sets
- explaining the rationale in research text for audit

The generator is not responsible for:

- bypassing validation
- choosing winners directly
- mutating live strategy ownership

### Search Process

The search process should be:

1. Build market context for each symbol.
2. Ask AI for a batch of candidate strategy definitions in DSL form.
3. Validate every generated strategy definition structurally and semantically.
4. Expand each valid candidate into nearby parameter variants.
5. Run backtests for every valid expanded candidate.
6. Keep only candidates with `return_percent > 50`.
7. Submit surviving candidates into the approval flow.

### Candidate Expansion

AI should generate the base strategy form. The system should generate additional variants around the AI proposal by perturbing:

- lookback windows
- stop loss and take profit bands
- hold time
- risk fraction
- threshold constants used in entry and exit comparisons

This keeps AI focused on idea generation while the platform does deterministic local search.

## Backtesting Model

### Score Definition

The only retention score in phase 1 is:

`backtest_return_percent`

This means cumulative return over the tested interval. A candidate passes the retention gate only if:

`backtest_return_percent > 50`

No drawdown, Sharpe-like stability, overfit risk, or trade count filter blocks a candidate in this phase.

### Backtest Output

Backtest output must include:

- `backtest_report_id`
- `strategy_def_id`
- `strategy_package_id`
- `symbol`
- `dataset_range`
- `sample_count`
- `trade_count`
- `net_pnl`
- `return_percent`
- `max_drawdown`
- `win_rate`
- `profit_factor`
- `stability_score`
- `overfit_risk`
- `generated_at`

Even though only `return_percent` is used for retention, the other fields should still be recorded for audit and future filtering phases.

### Semantics Alignment

The backtest engine and trader execution engine must interpret the DSL consistently. They do not need to share every line of code, but they must share:

- the same indicator definitions
- the same rule evaluation semantics
- the same entry and exit operator behavior
- the same one-symbol-one-active-strategy ownership logic

If these semantics diverge, backtest scores become untrustworthy for replacement decisions.

## Approval Flow Integration

### Required Persistence Path

All retained candidates must go through the current persisted audit path:

- `strategy_packages`
- `backtest_reports`
- `approval_records`

The approval chain remains mandatory. No candidate that merely passes the `> 50%` return gate may bypass approval and go live automatically.

### Approval Semantics

For each surviving candidate:

1. Save the candidate DSL and metadata as a strategy package.
2. Save the backtest report including `return_percent`.
3. Create an approval record using the current governor approval machinery.
4. If approved, publish a snapshot that references the approved strategy definition for the relevant symbol.

### Snapshot Evolution

`StrategyConfigSnapshot` currently focuses on global switches and guardrails. It must evolve to include per-symbol approved strategy bindings, for example:

- `symbol_strategy_bindings: dict[str, ApprovedStrategyBinding]`

Each binding should include:

- `strategy_def_id`
- `strategy_package_id`
- `backtest_report_id`
- `score`
- `score_basis`
- `approval_record_id`
- `activated_at`

Global controls such as `market_mode`, `risk_multiplier`, and `per_symbol_max_position` still remain on the snapshot.

## Live Execution Model

### One Active Strategy Per Symbol

At any point in time, each symbol has at most one active strategy owner. This ownership must cover:

- open position ownership
- open order ownership
- signal production ownership

The trader must maintain a runtime mapping:

- `symbol -> active_strategy_binding`

### Replacement Rule

An approved strategy may replace the current active strategy for a symbol only if:

`new_score >= current_score * 1.10`

Where both scores are measured using:

- `score_basis = backtest_return_percent`

If there is no active strategy for the symbol, the approved strategy can become active immediately subject to normal run mode and risk controls.

If there is an active strategy and the replacement threshold is not met, the candidate remains approved but inactive.

### Handover Sequence

If the replacement threshold is met, the trader must execute a controlled handover:

1. Mark the symbol as `handover_pending`.
2. Cancel all open orders owned by the current strategy.
3. Flatten the current position for that symbol.
4. Record the old strategy close reason as `replaced_by_stronger_strategy`.
5. Swap the active ownership binding to the new strategy.
6. Resume signal evaluation under the new strategy.
7. Allow the new strategy to open a position normally.

The system must never directly overlap old and new strategy positions on the same symbol.

### Failure Handling

If handover fails at any stage:

- the symbol enters a protected state
- the trader blocks new opens on that symbol
- a risk event is written
- the runtime remains recoverable from checkpoint state

No partial handover should silently continue trading.

## Recovery and State

### Runtime State Additions

Redis runtime state should track:

- active strategy binding per symbol
- inactive approved candidates per symbol
- symbol handover status
- last replacement decision basis

### PostgreSQL Audit Additions

PostgreSQL history should capture:

- strategy replacement decisions
- rejected replacement attempts due to `< 10%` improvement
- handover start and end events
- strategy close reason for replacement

### Recovery Semantics

Checkpoint and recovery logic must understand strategy ownership. On restart, recovery must verify:

- current symbol position
- open orders
- active strategy binding
- whether a handover was in progress

If the recovered exchange state does not match the stored strategy ownership state, the symbol should remain protected until reconciled.

## Validation Rules

Every AI-generated strategy definition must pass validation before backtesting:

- allowed operators only
- allowed indicators only
- valid directionality only
- required parameter references present
- numeric bounds checked
- symbol scope exactly one symbol in phase 1
- no recursive or unsupported expressions

Invalid generated strategies should be rejected with explicit error recording, not coerced silently.

## Testing Strategy

Implementation must be validated across five layers:

1. DSL validation tests
2. DSL interpreter unit tests
3. backtest scoring tests
4. governor pipeline tests for retention and approval submission
5. trader handover tests for single-symbol replacement behavior

Critical scenarios:

- AI generates structurally invalid DSL
- candidate return is `49.9` and is rejected
- candidate return is `50.1` and is retained
- no active strategy exists for symbol
- active strategy exists but new score is below the `10%` threshold
- active strategy exists and new score clears the threshold
- handover fails after cancel but before flatten
- restart occurs during handover

## Rollout Plan

The implementation should be staged:

### Phase 1

- introduce strategy DSL contracts
- generate DSL-based candidates
- backtest DSL-based candidates
- retain candidates with `return_percent > 50`
- submit them into approval
- no live trader adoption yet

### Phase 2

- extend snapshots with per-symbol approved strategy bindings
- make trader consume DSL strategies
- enforce one-active-strategy-per-symbol ownership

### Phase 3

- add stronger-strategy replacement with `10%` threshold
- persist handover state
- recovery support for handover

This phased rollout reduces the risk of changing research and live execution semantics at the same time.

## Risks and Tradeoffs

### Main Risks

- AI may generate many invalid or trivial strategies unless prompted and validated carefully.
- A single-metric optimization target can select brittle strategies.
- Backtest/live semantic drift can produce false confidence.
- Strategy handover adds operational complexity and recovery edge cases.

### Accepted Tradeoff In Phase 1

The user explicitly chose a simple retention rule: only `backtest_return_percent > 50`.

This is intentionally narrow and likely insufficient for durable production selection, but it is the correct phase-1 behavior because:

- it matches the requested optimization target exactly
- it keeps implementation scope bounded
- it avoids hidden filtering criteria that would contradict the agreed requirements

## Open Future Extensions

These are intentionally deferred:

- additional score dimensions such as drawdown or stability
- portfolio-level strategy arbitration across symbols
- multi-strategy layering per symbol
- richer feature libraries
- online re-ranking from live performance
- human-in-the-loop approval UX improvements

## Implementation Boundary

This design is intentionally focused on:

- representation
- generation
- backtesting
- approval submission
- execution ownership and replacement semantics

It does not prescribe UI work, alert wording, or operator dashboards beyond what is required to support audit and recovery.

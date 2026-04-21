# Source Architecture Cleanup Design

## Goal

Complete Stage 2 of the simplification by deleting source-level functionality that no longer participates in the production runtime. The system should become a fixed-strategy trader with notifier controls, Redis runtime state, Postgres persistence, and OKX connectivity.

## Remove

- Governor application and service code.
- AI/OpenAI/Codex helper code.
- Qdrant case store.
- Research package, approval, governance, and backtest contracts used only by governor.
- DSL strategy definition and rule execution modules.
- Tests that exist only for removed governor/research/DSL/Qdrant behavior.
- Redis/Postgres methods and tables that only support governor/research/approval.
- Notifier summaries that report governor health or governor-published snapshots.

## Keep

- `trader`, `notifier`, `momentum_backtest` apps.
- OKX REST and websocket adapters.
- Risk, execution, state, recovery, checkpoint logic.
- Fixed `StrategyConfigSnapshot` and `ApprovedStrategyBinding` because fixed strategy snapshots still use them.
- `ApprovalState` enum for snapshot approval state.
- Redis keys for current runtime mode, symbol summaries, active strategies, budget, fault flags, manual release, and fixed snapshot state.
- Postgres tables for orders, fills, positions, risk events, strategy snapshots, execution checkpoints, notification events, and strategy replacements.

## Expected Shape

After cleanup, imports under `src/xuanshu` should not reference:

- `xuanshu.governor`
- `xuanshu.infra.ai`
- `xuanshu.infra.storage.qdrant_store`
- `xuanshu.contracts.research`
- `xuanshu.contracts.approval`
- `xuanshu.contracts.governance`
- `xuanshu.contracts.backtest`
- `xuanshu.contracts.strategy_definition`
- `xuanshu.strategies.dsl_*`

The remaining `src/xuanshu/strategies` package should only contain runtime signal behavior that `trader` still uses, or be replaced by a simpler fixed-strategy signal module.

## Testing

Delete tests for removed modules. Update storage and notifier tests to cover only retained runtime behavior. Run the full test suite after each large deletion batch.

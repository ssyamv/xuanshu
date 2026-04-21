# Runtime Architecture Simplification Design

## Goal

Simplify the production runtime before adopting the ETH 4H volatility breakout strategy. Stage 1 removes unused runtime services and configuration while preserving the trader execution path, notifier controls, Redis runtime state, and Postgres persistence.

## Current State

As of 2026-04-21, the production runtime has converged on a fixed-strategy execution model:

- `trader` is the only process that connects to OKX and executes strategy decisions.
- `notifier` is the only operator command surface and writes manual runtime controls into Redis.
- `redis` is the hot state boundary for run mode, symbol summaries, fault flags, budget summary, and manual release requests.
- `postgres` is the durable runtime fact store for checkpoints, orders, fills, positions, risk events, and notification audit rows.
- `governor` and `qdrant` are no longer part of the compose runtime.
- fixed reviewed strategy snapshots are loaded through `XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH`, and trader startup prefers that file over any stale Redis snapshot.

The current local changes add explicit operator controls:

- `/pause [reason]` writes `halted` mode and a `manual_pause` fault flag.
- `/start [reason]` clears manual pause/takeover flags, requests `normal`, and sets a Redis manual release target.
- `/release <mode> [reason]` requests a less restrictive mode only if trader can safely apply it.
- `/capital <amount> [reason]` writes a manual strategy capital override into the Redis budget summary.
- trader synchronizes those controls before event dispatch and per-symbol evaluation, then publishes the resulting runtime state.

## Stage 1 Scope

Keep:

- `trader`
- `notifier`
- `redis`
- `postgres`
- fixed strategy snapshot loading through `XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH`

Remove from runtime packaging:

- `governor`
- `qdrant`
- OpenAI and Codex runtime configuration
- research provider runtime configuration
- Qdrant runtime configuration
- operation docs that instruct operators to run research, approval, or governor flows

## Non-Goals

- Do not delete governor source code in Stage 1.
- Do not delete research, approval, or DSL tests in Stage 1.
- Do not change live trading mode.
- Do not deploy a new active strategy in this stage.

Stage 2 will remove unused source modules and tests after the runtime shape is stable.

## Runtime Shape

The single-host compose stack becomes:

```text
trader
  depends_on redis, postgres

notifier
  depends_on redis, postgres

redis

postgres
```

`trader` remains responsible for OKX connectivity, state, risk, recovery, and execution. `notifier` remains responsible for Telegram notifications and manual control commands. Redis remains the hot state boundary. Postgres remains the durable runtime fact store.

## Runtime Control Flow

Manual controls flow through Redis rather than direct process calls:

```text
Telegram command
  -> notifier validates command and writes Redis runtime state
  -> trader reads Redis controls during event dispatch / symbol evaluation
  -> trader applies only safe relaxations, always accepts stricter modes
  -> trader writes refreshed mode, fault flags, symbol summaries, and budget summary
  -> notifier renders status from Redis plus durable Postgres facts
```

Rules:

- Stricter mode changes such as `halted` are applied immediately.
- Relaxing to `normal` or `degraded` requires an approved startup snapshot, clean checkpoint open-risk state, and no active fault flags.
- Invalid manual release targets are cleared by trader.
- Manual strategy capital overrides persist in the Redis budget summary with `manual_strategy_total_amount_override=true` and are reflected into trader `starting_nav` and risk-kernel NAV.
- Manual command audit events are stored in Postgres but are filtered out of proactive Telegram risk alerts.

## Configuration

The environment templates should only include values needed by the remaining runtime services:

- `XUANSHU_ENV`
- `XUANSHU_OKX_SYMBOLS`
- `XUANSHU_OKX_ACCOUNT_MODE`
- `XUANSHU_TRADER_STARTING_NAV`
- `XUANSHU_DEFAULT_RUN_MODE`
- `XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH`
- OKX credentials
- Telegram credentials
- Postgres DSN
- Redis URL

Production defaults stay protected with `XUANSHU_DEFAULT_RUN_MODE=halted`.

## Testing

Update deployment contract tests so they assert:

- compose does not define `governor` or `qdrant`
- compose does not reference OpenAI, Codex, Qdrant, or research provider variables
- compose still defines `trader`, `notifier`, `redis`, and `postgres`
- docs tell operators to read `trader` and `notifier` logs, not governor logs
- docs no longer describe research provider or committee approval operations
- notifier command tests cover `/help`, `/pause`, `/start`, `/capital`, and runtime-first `/positions`
- trader wiring tests cover Redis manual run-mode release and manual strategy capital synchronization

Run the full test suite after Stage 1.

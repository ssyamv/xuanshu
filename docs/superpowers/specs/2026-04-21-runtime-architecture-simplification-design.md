# Runtime Architecture Simplification Design

## Goal

Simplify the production runtime before adopting the ETH 4H volatility breakout strategy. Stage 1 removes unused runtime services and configuration while preserving the trader execution path, notifier controls, Redis runtime state, and Postgres persistence.

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

Run the full test suite after Stage 1.

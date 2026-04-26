# Single-Host Runbook

## Deploy

```bash
docker compose --env-file .env.prod up -d --build
```

## Preflight

```bash
env $(grep -v '^#' .env.prod | xargs) uv run python -m xuanshu.ops.preflight
```

## First Startup

- Keep `XUANSHU_DEFAULT_RUN_MODE=halted`.
- Verify dependencies and logs.
- Verify notifier command surface and recent checkpoint state.
- Verify the fixed strategy snapshot path with `XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH` before release.
- Only then allow trading mode release.

## Operator Commands

Telegram command handling is the supported operator control surface:

- `/help`: show supported commands.
- `/status`: show mode, fixed snapshot version, fault flags, equity, strategy total amount, strategy logic, and runtime symbol summaries.
- `/orders`: show recent durable order facts.
- `/risk`: show recent risk events without internal budget noise.
- `/pause [reason]`: request `halted` and record a `manual_pause` fault flag.
- `/start [reason]` or `/resume [reason]`: clear manual pause and legacy takeover flags, then request release to `normal`.
- `/entrygap`: show the live gap to entry conditions.
- `/withdraw <amount> [reason]`: transfer USDT from trading to funding.
- `/deposit <amount> [reason]`: transfer USDT from funding to trading.

Manual commands write Redis runtime controls and durable audit events. They do not call OKX directly. Trader reads those controls during event dispatch and symbol evaluation, then republishes the effective mode, budget summary, fault flags, and symbol summaries.

## Fixed Strategy

- Generate or copy a reviewed fixed strategy snapshot onto the server.
- Set `XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH` to that file.
- Keep the snapshot `market_mode=halted` until logs and checkpoint state are understood.
- Use the trader runtime as the only strategy execution path.
- Trader startup prefers the configured fixed snapshot over stale Redis snapshots.
- Strategy changes require replacing the reviewed fixed snapshot path, not issuing a Telegram command.

## Recovery

- If startup recovery fails, do not release trading.
- Inspect recent `risk_events`, `execution_checkpoints`, and notifier alerts.
- Keep the runtime in `halted` until state is understood.
- Use `/status`, `/orders`, and `/risk` to inspect the runtime view before release.
- Use `/start` only after the active fixed snapshot, checkpoint, and fault flags are clean.

## Rollback

- Stop the compose stack.
- Restore the previous code/config version.
- Start again with protected mode.
- Re-run preflight before allowing any normal trading.

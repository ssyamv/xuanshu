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

## Fixed Strategy

- Generate or copy a reviewed fixed strategy snapshot onto the server.
- Set `XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH` to that file.
- Keep the snapshot `market_mode=halted` until logs and checkpoint state are understood.
- Use the trader runtime as the only strategy execution path.

## Recovery

- If startup recovery fails, do not release trading.
- Inspect recent `risk_events`, `execution_checkpoints`, and notifier alerts.
- Keep the runtime in `halted` until state is understood.

## Rollback

- Stop the compose stack.
- Restore the previous code/config version.
- Start again with protected mode.
- Re-run preflight before allowing any normal trading.

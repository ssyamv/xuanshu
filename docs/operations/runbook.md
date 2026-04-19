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
- Only then allow trading mode release.

## Governor Research

- Research is a Governor responsibility, not a Trader responsibility.
- manual research is operator-led.
- schedule-driven research runs on the planned cadence.
- event-triggered research runs when a qualifying market or system event occurs.
- Only research with committee approval may influence execution snapshots.

## Recovery

- If startup recovery fails, do not release trading.
- Inspect recent `risk_events`, `execution_checkpoints`, and notifier alerts.
- Keep the runtime in `halted` until state is understood.

## Rollback

- Stop the compose stack.
- Restore the previous code/config version.
- Start again with protected mode.
- Re-run preflight before allowing any normal trading.

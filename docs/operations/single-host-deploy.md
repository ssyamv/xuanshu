# Single-Host Deploy

## Canonical Entrypoint

Use only the compose entrypoint below for single-host deployment:

```bash
docker compose --env-file .env.prod up -d --build
```

## First Startup Rule

- Set `XUANSHU_DEFAULT_RUN_MODE=halted` in the production environment file.
- Verify Redis, PostgreSQL, OKX credentials, Telegram reachability, and the fixed strategy snapshot file before allowing normal trading.
- Keep operator control explicit: start protected first, then release trading mode deliberately after checks pass.
- Use `XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH` for the reviewed active strategy.
- Do not deploy or operate `governor` / `qdrant` as part of the current runtime.

## Runtime Services

The expected compose runtime is:

- `trader`
- `notifier`
- `redis`
- `postgres`

`trader` owns OKX connectivity, fixed strategy loading, risk checks, execution, recovery, checkpoints, and Redis runtime publication. `notifier` owns Telegram command handling and notifications. Redis is the hot state/control boundary, and PostgreSQL is the durable fact store.

## Release After Deploy

After deploy, use Telegram:

- `/status` to verify snapshot version, mode, fault flags, account equity, strategy total amount, strategy logic, and runtime summaries.
- `/risk` and `/orders` to verify recent durable facts.
- `/start <reason>` only after checks pass.

`/pause` is expected to take effect immediately through Redis. Mode relaxation is requested with `/start` and only applied by trader when the active snapshot is approved, checkpoint state allows new risk, and fault flags are clean.

## Notes

- The app services are expected to run under `restart: unless-stopped`.
- Keep `.env.prod` local to the server and do not commit it.

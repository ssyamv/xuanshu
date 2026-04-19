# Single-Host Deploy

## Canonical Entrypoint

Use only the compose entrypoint below for single-host deployment:

```bash
docker compose --env-file .env.prod up -d --build
```

## First Startup Rule

- Set `XUANSHU_DEFAULT_RUN_MODE=halted` in the production environment file.
- Verify Redis, PostgreSQL, Qdrant, OKX credentials, OpenAI credentials, and Telegram reachability before allowing normal trading.
- Keep operator control explicit: start protected first, then release trading mode deliberately after checks pass.

## Notes

- The app services are expected to run under `restart: unless-stopped`.
- Keep `.env.prod` local to the server and do not commit it.

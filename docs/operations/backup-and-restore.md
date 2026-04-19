# Backup And Restore

## PostgreSQL Backup

Run on the server:

```bash
docker compose exec -T postgres pg_dump -U xuanshu -d xuanshu > backup-xuanshu.sql
```

## PostgreSQL Restore

```bash
cat backup-xuanshu.sql | docker compose exec -T postgres psql -U xuanshu -d xuanshu
```

## Redis Persistence Check

Redis is configured with append-only persistence in `docker-compose.yml`. Verify the volume is mounted and persistence files exist under `/data`.

## Restore Rule

- Keep the system in `halted` while restoring.
- Run preflight after restore.
- Check latest checkpoint, latest snapshot, current mode, and recent risk events before releasing trading.

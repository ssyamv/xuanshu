# Logging

## Runtime Log Format

The app entrypoints emit JSON logs to stderr with these fields:

- `timestamp`
- `level`
- `logger`
- `event`
- service-specific fields such as `service`, `mode`, `status`, `snapshot_version`

## Minimum Usage

- Start services with the canonical compose command.
- Read service logs with `docker compose logs -f trader notifier`.
- Run the preflight check before release:

```bash
uv run python -m xuanshu.ops.preflight
```

## Minimum Events To Watch

- `runtime_started`
- `runtime_failed`
- `command_delivery_failed`
- `startup_notification_failed`

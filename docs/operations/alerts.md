# Alerts

These alerts are the minimum required for single-host 1000u live operation.

## Critical

- `startup_recovery_failed`
- runtime mode enters `halted`
- repeated runtime mode escalation into `reduce_only`
- PostgreSQL unavailable
- Redis unavailable

## Warning

- notifier delivery failures repeating
- OKX stream instability or repeated reconnect pressure
- fixed strategy snapshot missing or malformed

## Operator Action

- For `startup_recovery_failed`: keep the system in protected mode and investigate checkpoint/exchange mismatch before allowing normal trading.
- For `halted`: inspect current snapshot, recent risk events, and last recovery result before releasing trading again.
- For repeated `reduce_only`: treat as degraded live state, reduce confidence in automation, and be ready to issue `/takeover halted`.
- For fixed strategy snapshot issues: restore the last reviewed snapshot or leave `XUANSHU_DEFAULT_RUN_MODE=halted`.

# Alerts

These alerts are the minimum required for single-host 1000u live operation.

## Critical

- `startup_recovery_failed`
- runtime mode enters `halted`
- `manual_pause_requested`
- repeated runtime mode escalation into `reduce_only`
- PostgreSQL unavailable
- Redis unavailable

## Warning

- notifier delivery failures repeating
- OKX stream instability or repeated reconnect pressure
- fixed strategy snapshot missing or malformed
- manual release requests that do not take effect because checkpoint or fault state is not clean

## Operator Action

- For `startup_recovery_failed`: keep the system in protected mode and investigate checkpoint/exchange mismatch before allowing normal trading.
- For `halted`: inspect `/status`, current snapshot, recent risk events, and last recovery result before releasing trading again.
- For `manual_pause_requested`: confirm the pause reason, inspect `/positions` and `/orders`, then leave the system halted until the operator explicitly requests `/start` or `/release`.
- For repeated `reduce_only`: treat as degraded live state, reduce confidence in automation, and be ready to issue `/pause`.
- For fixed strategy snapshot issues: restore the last reviewed snapshot or leave `XUANSHU_DEFAULT_RUN_MODE=halted`.
- For capital changes: verify `/status` shows the expected `策略总金额` and that trader republished the budget summary before allowing new risk.

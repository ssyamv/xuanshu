# Alerts

These alerts are the minimum required for single-host 1000u live operation.

## Critical

- `startup_recovery_failed`
- runtime mode enters `halted`
- repeated runtime mode escalation into `reduce_only`
- PostgreSQL unavailable
- Redis unavailable
- Qdrant unavailable during governor cycle

## Warning

- governor consecutive failures increasing
- notifier delivery failures repeating
- OKX stream instability or repeated reconnect pressure
- research pipeline waiting on committee approval
- research provider failure during Governor slow-path analysis

## Operator Action

- For `startup_recovery_failed`: keep the system in protected mode and investigate checkpoint/exchange mismatch before allowing normal trading.
- For `halted`: inspect current snapshot, recent risk events, and last recovery result before releasing trading again.
- For repeated `reduce_only`: treat as degraded live state, reduce confidence in automation, and be ready to issue `/takeover halted`.
- For manual research, event-triggered research, or schedule-driven research: keep the resulting package out of execution until committee approval has been recorded.
- For research provider failure: inspect `governor_runs` audit fields for `research_provider`, `research_status`, and `research_error`; there is no automatic fallback.
- For `codex_cli` failures: verify the selected host still has a valid `codex login` session before retrying research.

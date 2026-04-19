# Single-Host Launch Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the repo ready for single-host 1000u live deployment by fixing deployment entrypoints, production config assets, startup protection, and operator-facing run procedures.

**Architecture:** Keep the existing single-host `docker compose` topology and current service boundaries. Add only the minimum launch-readiness assets around it: a production config template, deterministic deployment commands, guarded startup defaults, and operator docs/scripts that validate the environment before allowing the system to run.

**Tech Stack:** Python 3.12, `pydantic-settings`, `pytest`, Docker Compose, Markdown docs

---

### Task 1: Fixed Deployment And Production Config

**Files:**
- Create: `.env.prod.example`
- Create: `docs/operations/single-host-deploy.md`
- Modify: `docker-compose.yml`
- Modify: `tests/apps/test_trader_app_wiring.py`

- [ ] **Step 1: Write the failing deployment/config tests**

Add tests that lock these requirements:

```python
def test_single_host_deploy_contract_lists_prod_env_template() -> None:
    prod_env = Path(".env.prod.example").read_text(encoding="utf-8")
    assert "XUANSHU_ENV=prod" in prod_env
    assert "XUANSHU_DEFAULT_RUN_MODE=halted" in prod_env
    assert "OKX_API_KEY=" in prod_env
    assert "OPENAI_API_KEY=" in prod_env
    assert "TELEGRAM_BOT_TOKEN=" in prod_env


def test_single_host_deploy_doc_pins_compose_entrypoint() -> None:
    deploy_doc = Path("docs/operations/single-host-deploy.md").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "docker compose --env-file .env.prod up -d --build" in deploy_doc
    assert "restart: unless-stopped" in compose
```
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/apps/test_trader_app_wiring.py -q
```

Expected: FAIL because `.env.prod.example` and `docs/operations/single-host-deploy.md` do not exist yet.

- [ ] **Step 3: Write the minimal deployment/config assets**

Create `.env.prod.example` with the exact launch-time variables:

```dotenv
XUANSHU_ENV=prod
XUANSHU_OKX_SYMBOLS=BTC-USDT-SWAP,ETH-USDT-SWAP
XUANSHU_TRADER_STARTING_NAV=1000
XUANSHU_DEFAULT_RUN_MODE=halted
OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=
OPENAI_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
POSTGRES_DSN=postgresql+psycopg://xuanshu:xuanshu@postgres:5432/xuanshu
REDIS_URL=redis://redis:6379/0
QDRANT_URL=http://qdrant:6333
```

Create `docs/operations/single-host-deploy.md` with the canonical command sequence:

```markdown
# Single-Host Deploy

## Canonical Entrypoint

Use only:

```bash
docker compose --env-file .env.prod up -d --build
```

## First Startup Rule

- `XUANSHU_DEFAULT_RUN_MODE=halted`
- Verify dependencies and notifier reachability first
- Only then allow operator-driven mode release
```
```

If `docker-compose.yml` does not already communicate single-host long-running intent, keep `restart: unless-stopped` on the three app services.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/apps/test_trader_app_wiring.py -q
```

Expected: PASS with the new deployment/config contract tests included.

- [ ] **Step 5: Commit**

```bash
git add .env.prod.example docs/operations/single-host-deploy.md docker-compose.yml tests/apps/test_trader_app_wiring.py
git commit -m "feat: add single-host production deploy contract"
```

### Task 2: Startup Protection And Preflight Check

**Files:**
- Create: `src/xuanshu/ops/preflight.py`
- Create: `tests/ops/test_preflight.py`
- Modify: `src/xuanshu/config/settings.py`
- Modify: `src/xuanshu/apps/trader.py`

- [ ] Add a setting for default startup run mode, defaulting to `halted` in production assets.
- [ ] Add a preflight checker that validates required env/config and dependency reachability.
- [ ] Ensure startup is operator-safe by default.
- [ ] Verify with focused pytest coverage.

### Task 3: Minimal Observability Surface

**Files:**
- Create: `docs/operations/alerts.md`
- Create: `docs/operations/logging.md`
- Modify: relevant app entrypoints for structured startup/error logging
- Modify: relevant tests

- [ ] Define minimal operator-facing logs and alert conditions.
- [ ] Add startup/runtime logging for service health and failure transitions.
- [ ] Verify critical transitions are visible without reading raw Python tracebacks.

### Task 4: Backup, Recovery, And Operator Runbooks

**Files:**
- Create: `docs/operations/backup-and-restore.md`
- Create: `docs/operations/runbook.md`

- [ ] Document Postgres backup/restore commands.
- [ ] Document restart/reconcile/takeover procedures.
- [ ] Document rollback procedure for single-host deployment.

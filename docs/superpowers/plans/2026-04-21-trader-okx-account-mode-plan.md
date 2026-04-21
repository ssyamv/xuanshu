# Trader OKX Account Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit OKX demo/live account switch for the trader service, deploy the current production environment in demo mode on `xuanshu-prod-01`, and verify the governor -> trader -> notifier runtime path end to end.

**Architecture:** Add a typed trader runtime setting for OKX account mode and thread it into the OKX REST and private websocket adapters so exchange-environment selection is centralized instead of hard-coded. Expose the setting in compose and env templates, then deploy by updating the remote `.env.prod`, rebuilding services, and validating runtime state propagation across all three services.

**Tech Stack:** Python, Pydantic Settings, OKX REST/WebSocket adapters, pytest, Docker Compose, SSH

---

### Task 1: Define Trader Account Mode Contract

**Files:**
- Modify: `src/xuanshu/core/enums.py`
- Modify: `src/xuanshu/config/settings.py`
- Test: `tests/contracts/test_contracts.py`

- [ ] Add a failing settings contract test that proves trader runtime accepts `XUANSHU_OKX_ACCOUNT_MODE=demo` and defaults to `live`.
- [ ] Run: `pytest tests/contracts/test_contracts.py -k okx_account_mode -v`
- [ ] Add the minimal enum and settings fields needed to make the test pass.
- [ ] Re-run: `pytest tests/contracts/test_contracts.py -k okx_account_mode -v`

### Task 2: Thread Account Mode Into OKX Adapters

**Files:**
- Modify: `src/xuanshu/infra/okx/rest.py`
- Modify: `src/xuanshu/infra/okx/private_ws.py`
- Modify: `src/xuanshu/apps/trader.py`
- Test: `tests/execution/test_okx_execution_engine.py`
- Test: `tests/apps/test_trader_app_wiring.py`

- [ ] Add failing adapter tests that prove demo mode adds the OKX simulated-trading marker on REST requests and private websocket login/connect setup, while live mode does not.
- [ ] Run: `pytest tests/execution/test_okx_execution_engine.py -k simulated -v`
- [ ] Add a failing trader wiring test that proves `build_trader_runtime()` threads the selected account mode into the REST and private websocket clients.
- [ ] Run: `pytest tests/apps/test_trader_app_wiring.py -k account_mode -v`
- [ ] Implement the minimal adapter and wiring changes to satisfy both failing test groups.
- [ ] Re-run: `pytest tests/execution/test_okx_execution_engine.py -k simulated -v`
- [ ] Re-run: `pytest tests/apps/test_trader_app_wiring.py -k account_mode -v`

### Task 3: Expose Deployment Configuration

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `.env.prod.example`
- Test: `tests/apps/test_trader_app_wiring.py`

- [ ] Add a failing deployment contract test that requires the new trader account mode variable in compose and env templates.
- [ ] Run: `pytest tests/apps/test_trader_app_wiring.py -k account_mode_contract -v`
- [ ] Implement the minimal compose and env-template changes.
- [ ] Re-run: `pytest tests/apps/test_trader_app_wiring.py -k account_mode_contract -v`

### Task 4: Run Local Regression Verification

**Files:**
- Test only

- [ ] Run the focused suite: `pytest tests/contracts/test_contracts.py tests/execution/test_okx_execution_engine.py tests/apps/test_trader_app_wiring.py -v`
- [ ] If any failures are unrelated to this change, stop and inspect before proceeding to deployment.

### Task 5: Deploy Demo Mode To xuanshu-prod-01

**Files:**
- Remote only: production checkout and `.env.prod`

- [ ] Inspect the remote checkout location, branch, and current `.env.prod` contents needed for deployment.
- [ ] Update remote `.env.prod` so `XUANSHU_OKX_ACCOUNT_MODE=demo`.
- [ ] Rebuild and restart with `docker compose --env-file .env.prod up -d --build`.
- [ ] Capture `docker compose ps` and recent service logs for `trader`, `governor`, and `notifier`.

### Task 6: Verify Cross-Service Runtime Path

**Files:**
- Remote only

- [ ] Verify preflight or equivalent runtime checks on the remote host with production env loaded.
- [ ] Confirm `governor` is publishing runtime artifacts the trader can consume.
- [ ] Confirm `trader` starts in demo account mode and completes OKX auth/query path without environment mismatch errors.
- [ ] Confirm `notifier` can observe and report the shared runtime state.
- [ ] Record any residual blockers, especially if remote credentials or external dependencies prevent a full exchange round-trip.

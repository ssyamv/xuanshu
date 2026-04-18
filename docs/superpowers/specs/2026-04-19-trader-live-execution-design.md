# Trader Live Execution Design

## 1. Scope

This spec defines the first unfinished sub-project from the live-core detailed design: make `Trader Service` a real live execution service against `OKX`, with real websocket inputs, real REST execution, real recovery, real Redis hot-state publication, and real PostgreSQL fact persistence.

This spec is intentionally limited to `Trader Service`. It does not implement:

- `Governor Service` input builder, expert layer, or real AI execution
- `Notifier Service` query surface or notification orchestration
- cross-service recovery workflows beyond the trader-owned boundaries
- deep Qdrant retrieval or case-based governance

The immediate objective is to turn the current trader runtime from "bootstraps settings, reads snapshot, applies startup gating, then blocks" into a minimal but production-shaped live trading loop.

## 2. Goals

The trader implementation produced from this spec must satisfy these goals:

- connect to real `OKX` public and private websocket channels
- support any configured `USDT-SWAP` instruments, with validation focused on `BTC/ETH`
- allow real order placement when valid real credentials are configured
- run startup recovery before allowing new risk
- consume live market events, maintain state, evaluate signals, enforce deterministic risk checks, and place real orders
- ingest exchange order and position updates back into state
- publish hot state into `Redis`
- persist execution facts and checkpoints into `PostgreSQL`
- automatically tighten mode to `reduce_only` or `halted` when state safety is compromised

## 3. Architecture

The trader is implemented as one process with explicit internal module boundaries. The process keeps the existing split between infrastructure adapters, deterministic domain modules, and app composition, but it introduces trader-specific orchestration modules so side effects, state updates, and recovery logic do not collapse into `apps/trader.py`.

The main runtime pipeline is:

1. `OKX Gateway` receives public and private messages
2. messages are decoded into standard trader events
3. `Event Dispatcher` routes each event
4. `State Engine` updates in-memory truth
5. market state triggers `Regime Router -> Signal Factory -> Risk Kernel`
6. allowed actions pass to `Execution Coordinator`
7. `Execution Coordinator` performs real REST execution and tracks in-flight intents
8. exchange acknowledgements and fills re-enter the dispatcher
9. Redis hot summaries and PostgreSQL facts are written from trader-owned state transitions

Two design rules are fixed:

- `Execution Engine` remains pure and only builds execution intents and payloads
- all network side effects live in infrastructure adapters or `Execution Coordinator`

## 4. Module Boundaries

### 4.1 OKX Gateway

The gateway owns websocket login, subscription, reconnect, heartbeat, and raw payload normalization.

Responsibilities:

- subscribe to public market channels for configured symbols
- subscribe to private order, position, and account channels
- decode exchange payloads into trader events
- tag disconnects, reconnects, and protocol faults as explicit fault events

Non-responsibilities:

- business decisions
- risk checks
- order intent generation

### 4.2 Event Dispatcher

The dispatcher is the only module allowed to fan out incoming runtime events. It maps event types to:

- `StateEngine` updates
- signal evaluation triggers
- execution update handling
- recovery escalation
- persistence writes

This module keeps event routing explicit so the runtime does not become a chain of hidden callbacks.

### 4.3 State Engine

The state engine becomes the in-memory trader truth. It must own:

- `market_state_by_symbol`
- `position_state_by_symbol`
- `open_order_state_by_symbol`
- `budget_state_by_symbol`
- `current_run_mode`
- `fault_flags`
- `last_public_stream_marker`
- `last_private_stream_marker`

It must provide snapshot builders for:

- symbol-level market evaluation
- runtime hot-state summaries for Redis
- checkpoint materialization

It must not issue exchange requests.

### 4.4 Regime Router, Signal Factory, Risk Kernel

These remain deterministic and synchronous from the trader's perspective.

- `Regime Router` classifies current symbol regime
- `Signal Factory` converts current state plus enabled strategy config into `CandidateSignal` values
- `Risk Kernel` produces the final `RiskDecision`

No AI calls are allowed in this chain.

### 4.5 Execution Engine

`Execution Engine` remains a pure function layer. It is responsible for:

- client order id generation
- translating `RiskDecision` into execution intents
- constructing validated OKX payloads for place/cancel/amend operations

It must not open sockets, call REST, or mutate runtime state.

### 4.6 Execution Coordinator

`Execution Coordinator` is the trader's side-effect orchestration boundary.

Responsibilities:

- accept allowed execution intents
- call OKX REST for place/cancel/amend actions
- maintain in-flight order intent correlation
- enforce idempotent retries by `client_order_id`
- schedule timeout cancel flows where required
- accept exchange acknowledgements and fills and map them back to tracked intents

This module is where live trading risk is operationally contained. It owns "what we asked the exchange to do" but not the authoritative portfolio truth, which still lives in `StateEngine`.

### 4.7 Recovery Supervisor

`Recovery Supervisor` owns startup and fault-triggered recovery.

Responsibilities:

- load latest checkpoint
- fetch current orders, positions, and account summaries via REST
- compare exchange truth to checkpoint and current local state
- decide whether the runtime can enter `normal`, `degraded`, `reduce_only`, or `halted`
- emit recovery facts for persistence and follow-up notification

It must block new risk until reconciliation succeeds.

## 5. File Plan

The implementation should follow the current repository layout and extend it with focused trader runtime files.

Modify:

- `src/xuanshu/apps/trader.py`
- `src/xuanshu/state/engine.py`
- `src/xuanshu/execution/engine.py`
- `src/xuanshu/infra/okx/public_ws.py`
- `src/xuanshu/infra/okx/private_ws.py`
- `src/xuanshu/infra/okx/rest.py`
- `src/xuanshu/infra/storage/redis_store.py`
- `src/xuanshu/infra/storage/postgres_store.py`
- `src/xuanshu/config/settings.py`

Create:

- `src/xuanshu/trader/dispatcher.py`
- `src/xuanshu/trader/recovery.py`
- `src/xuanshu/execution/coordinator.py`
- `src/xuanshu/contracts/events.py`

Tests should be added under:

- `tests/apps/`
- `tests/trader/`
- `tests/execution/`
- `tests/storage/`
- adapter tests under `tests/contracts/` or new OKX-focused test files if needed

## 6. Runtime Data Flow

### 6.1 Startup

Startup order is fixed:

1. load `TraderRuntimeSettings`
2. validate configured symbols as `USDT-SWAP`
3. load latest effective strategy snapshot from `Redis`
4. load latest execution checkpoint from `PostgreSQL`
5. run startup recovery using REST order, position, and account snapshots
6. derive safe runtime mode
7. publish current mode and runtime summary to `Redis`
8. connect public and private websocket streams
9. begin main event loop

If steps 4-6 cannot establish a safe known state, the trader starts in `halted` and refuses new risk.

### 6.2 Live Evaluation

For every symbol:

1. market events update state
2. a symbol snapshot is built
3. regime classification runs
4. enabled strategy logic emits zero or more `CandidateSignal` values
5. `RiskKernel` evaluates each candidate against:
   - current mode
   - configured snapshot restrictions
   - symbol and portfolio budgets
   - current position and open orders
   - fault flags
6. allowed decisions become execution intents
7. `ExecutionCoordinator` submits the REST requests

### 6.3 Execution Feedback

Private stream and REST acknowledgements feed back into the runtime:

1. order accepted or rejected event updates in-flight intent status
2. fill events update order and position truth
3. state transitions write:
   - Redis hot summaries
   - PostgreSQL order/fill/position facts
4. checkpoint generation runs on significant execution transitions and on a periodic cadence

## 7. State and Persistence

### 7.1 Redis Hot State

The trader must publish at least:

- latest effective strategy snapshot reference
- current run mode
- symbol runtime summary
- budget pool summary
- active fault flags

Redis is used for hot reads and recovery assistance only. It is not the final audit source.

### 7.2 PostgreSQL Facts

The trader must persist minimal write paths for:

- `orders`
- `fills`
- `positions`
- `risk_events`
- `execution_checkpoints`

These writes can initially be append-oriented and minimal, but they must be real writes through a concrete storage boundary, not placeholder constants.

## 8. Failure Strategy

The runtime must use conservative failure handling.

### 8.1 Market Data Faults

- temporary public stream disconnect: set fault flag, remain running
- prolonged public stream disconnect: degrade to at least `degraded`

### 8.2 Private State Faults

- private stream disconnect: block new risk and move to at least `reduce_only`
- private stream restored: recovery path must re-check exchange truth before new risk resumes

### 8.3 Execution Faults

- REST place/cancel/amend failure: persist risk event, keep correlation state, do not blindly duplicate exposure
- repeated execution failure on the same symbol: tighten symbol eligibility and possibly global mode

### 8.4 Reconciliation Faults

- checkpoint mismatch or position/order mismatch: move to `reduce_only`
- unresolved mismatch after recovery attempt: move to `halted`

### 8.5 Storage Faults

- Redis failure: continue locally, mark degraded visibility
- PostgreSQL failure: mark persistence fault and tighten mode if checkpoint integrity is affected

## 9. Security and Live Trading Guardrails

This sub-project explicitly allows real order placement.

Guardrails still required:

- only configured `USDT-SWAP` symbols are tradable
- all new risk must pass deterministic `RiskKernel`
- startup recovery must succeed before any new risk is opened
- any unresolved state mismatch must tighten mode
- every outbound order must carry a deterministic `client_order_id`

No separate "paper mode" is introduced in this spec because the user explicitly chose real trading by default.

## 10. Acceptance Criteria

This sub-project is complete only when all of the following are true:

1. `Trader` can connect to real OKX public and private streams for configured `USDT-SWAP` symbols.
2. Startup runs a real recovery sequence against latest checkpoint and exchange truth before allowing new risk.
3. Live market events can drive state updates, regime evaluation, signal generation, and risk decisions.
4. Allowed actions create real OKX execution requests with deterministic idempotent order ids.
5. Order and position updates re-enter the runtime and converge state.
6. Trader publishes Redis hot summaries for mode and symbol runtime state.
7. Trader persists minimal execution facts and checkpoints into PostgreSQL.
8. Disconnects, mismatches, or unresolved recovery failures force the runtime into `reduce_only` or `halted`.

## 11. Out of Scope

The following remain outside this spec and should be handled by later sub-projects:

- governor expert decomposition and real AI committee logic
- notifier command/query surface
- trader-to-notifier event publishing beyond persistence-ready facts
- multi-venue support
- derivatives beyond `USDT-SWAP`
- advanced portfolio netting or cross-strategy optimization

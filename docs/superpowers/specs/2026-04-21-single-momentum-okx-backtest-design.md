# Single Momentum OKX Backtest Design

## Goal

Simplify Xuanshu from an AI-driven strategy research and strategy-pool system into one fixed, explainable momentum strategy selected by OKX historical backtesting.

The first implementation should produce one deployable strategy for `BTC-USDT-SWAP` using `1H` candles. It should not depend on AI, strategy generation, committee approval, or a dynamic strategy pool.

## Non-Goals

- No AI strategy generation.
- No automatic governor strategy publication.
- No multi-strategy portfolio selection.
- No ETH strategy in the first version.
- No short strategy in the first version.
- No live-mode release as part of the initial backtest implementation.

## Safety Posture

Before changing the strategy path, the production server should be put into a protected state. The current observed remote configuration is `prod`, `live`, and `normal`, while the trader log reports `halted`. The implementation should make the deployment state explicit by setting the production runtime to `halted` before testing new strategy behavior.

Demo mode can be enabled separately if we want to validate OKX connectivity and execution without live trading. The first implementation does not require live trading.

## Architecture

The existing trader execution, state, checkpoint, Redis, Postgres, and OKX client infrastructure should remain in place. The change is scoped to strategy sourcing and backtesting:

1. Add an OKX historical candle loader that uses the existing public REST history endpoint.
2. Add a momentum backtest service that evaluates a small parameter grid on historical candles.
3. Add a command-line app that runs the backtest and writes the best strategy snapshot.
4. Teach trader startup to prefer a fixed configured strategy snapshot when present.
5. Keep governor and AI out of the active strategy path.

This keeps the operational foundation intact while removing the research and approval machinery from the critical path.

## Strategy Definition

The first strategy is long-only momentum:

- Symbol: `BTC-USDT-SWAP`
- Bar: `1H`
- Entry: price momentum over a configurable lookback is positive enough to enter long.
- Exit: stop loss, take profit, or max holding time.
- Position sizing: use existing risk controls and a conservative strategy-level risk fraction.

The initial parameter grid should be intentionally small:

- lookback candles: `12`, `24`, `48`, `72`
- stop loss bps: `100`, `200`, `300`
- take profit bps: `200`, `400`, `600`
- max hold minutes: `360`, `720`, `1440`

The strategy should generate one winning configuration, not a ranked live pool.

## Backtest Selection

The backtest should evaluate each parameter set using OKX historical candles and compute:

- sample count
- trade count
- return percent
- max drawdown
- win rate
- profit factor
- stability score

The selected strategy must pass minimum gates:

- at least 30 trades
- positive return
- profit factor greater than 1
- max drawdown within a conservative configured threshold

If no candidate passes, the command must fail without writing an active live strategy.

## Data Flow

1. Operator runs the momentum backtest command.
2. Command fetches OKX historical candles for `BTC-USDT-SWAP`.
3. Backtest service evaluates the grid.
4. The best passing result is converted into a fixed strategy snapshot.
5. The snapshot is written to a local JSON file.
6. Trader startup loads that fixed snapshot and uses it instead of governor-published strategy research.

The fixed snapshot should be explicit enough that an operator can inspect the chosen parameters before deploying it.

## Deployment Behavior

Governor does not need to run for strategy selection in this mode. It may remain available for future monitoring work, but it must not be required to publish the active strategy.

Production deployment should start protected:

- `XUANSHU_DEFAULT_RUN_MODE=halted`
- no live release until the generated strategy file is reviewed
- demo validation preferred before any live mode

## Error Handling

- OKX candle fetch failures should fail the backtest command with a clear message.
- Invalid or insufficient candle data should fail without producing a strategy snapshot.
- No passing parameter set should fail without modifying the previous active strategy file.
- Trader startup should reject malformed fixed strategy files and fall back only to a safe bootstrap halted snapshot.

## Testing

Add tests for:

- OKX candle normalization and pagination boundaries.
- Momentum backtest candidate evaluation.
- Selection gates when no candidate qualifies.
- Strategy snapshot serialization.
- Trader startup loading a fixed strategy snapshot.
- Governor/AI not being required for the fixed-strategy path.

Integration testing should use deterministic fixture candles, not live OKX network calls. Live OKX fetching can be manually verified through the command.

## Acceptance Criteria

- A local command can fetch OKX history and backtest the BTC 1H momentum grid.
- The command writes one fixed strategy snapshot only when a candidate passes gates.
- Trader can start from that fixed snapshot without AI or governor strategy generation.
- Production can be placed into halted mode before deployment testing.
- Existing execution, recovery, and risk tests continue to pass.

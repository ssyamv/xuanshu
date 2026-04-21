from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
import sys

from xuanshu.infra.okx.rest import OkxRestClient
from xuanshu.momentum.backtest import (
    MomentumBacktestConfig,
    MomentumBacktestResult,
    MomentumParameterSet,
    build_momentum_snapshot,
    evaluate_momentum_candidate,
    select_best_candidate,
)
from xuanshu.momentum.okx_history import fetch_okx_history_rows

_OKX_REST_BASE_URL = "https://www.okx.com"
_PARAMETER_GRID = (
    MomentumParameterSet(lookback=12, stop_loss_bps=100, take_profit_bps=200, max_hold_minutes=360),
    MomentumParameterSet(lookback=12, stop_loss_bps=100, take_profit_bps=400, max_hold_minutes=720),
    MomentumParameterSet(lookback=24, stop_loss_bps=200, take_profit_bps=400, max_hold_minutes=720),
    MomentumParameterSet(lookback=24, stop_loss_bps=200, take_profit_bps=600, max_hold_minutes=1440),
    MomentumParameterSet(lookback=48, stop_loss_bps=300, take_profit_bps=600, max_hold_minutes=1440),
    MomentumParameterSet(lookback=72, stop_loss_bps=300, take_profit_bps=600, max_hold_minutes=1440),
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest a fixed BTC momentum strategy using OKX history")
    parser.add_argument("--symbol", default="BTC-USDT-SWAP")
    parser.add_argument("--bar", default="1H")
    parser.add_argument("--limit", type=int, default=4320)
    parser.add_argument("--output", default="configs/active_strategy.json")
    parser.add_argument("--min-trades", type=int, default=30)
    parser.add_argument("--max-drawdown", type=float, default=20.0)
    return parser.parse_args(argv)


async def run_backtest(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = MomentumBacktestConfig(
        min_trade_count=args.min_trades,
        max_drawdown_percent=args.max_drawdown,
        risk_fraction=0.25,
    )
    client = OkxRestClient(base_url=_OKX_REST_BASE_URL, api_key="")
    try:
        rows = await fetch_okx_history_rows(client, symbol=args.symbol, bar=args.bar, limit=args.limit)
    finally:
        await client.aclose()

    results = [
        evaluate_momentum_candidate(parameters, rows, risk_fraction=config.risk_fraction)
        for parameters in _PARAMETER_GRID
    ]
    selected = select_best_candidate(results, config=config)
    if selected is None:
        _print_summary(results=results, selected=None)
        return 2

    snapshot = build_momentum_snapshot(
        selected=selected,
        symbol=args.symbol,
        generated_at=datetime.now(UTC),
        config=config,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    tmp_path.replace(output_path)
    _print_summary(results=results, selected=selected)
    print(f"wrote fixed strategy snapshot: {output_path}")
    return 0


def main() -> int:
    return asyncio.run(run_backtest())


def _print_summary(
    *,
    results: list[MomentumBacktestResult],
    selected: MomentumBacktestResult | None,
) -> None:
    for result in sorted(results, key=lambda item: item.stability_score, reverse=True):
        print(
            "candidate "
            f"lookback={result.parameters.lookback} "
            f"sl={result.parameters.stop_loss_bps} "
            f"tp={result.parameters.take_profit_bps} "
            f"hold={result.parameters.max_hold_minutes} "
            f"trades={result.trade_count} "
            f"return={result.return_percent:.4f}% "
            f"drawdown={result.max_drawdown_percent:.4f}% "
            f"pf={result.profit_factor:.4f} "
            f"stability={result.stability_score:.4f}"
        )
    if selected is None:
        print("no momentum candidate passed selection gates", file=sys.stderr)
        return
    print(
        "selected "
        f"lookback={selected.parameters.lookback} "
        f"sl={selected.parameters.stop_loss_bps} "
        f"tp={selected.parameters.take_profit_bps} "
        f"hold={selected.parameters.max_hold_minutes}"
    )


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
import sys

from xuanshu.infra.okx.rest import OkxRestClient
from xuanshu.momentum.okx_history import fetch_okx_history_rows
from xuanshu.vol_breakout.backtest import (
    VolBreakoutConfig,
    VolBreakoutParameters,
    build_vol_breakout_snapshot,
    candidate_passes,
    evaluate_vol_breakout,
)

_OKX_REST_BASE_URL = "https://www.okx.com"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest and export the fixed ETH 4H volatility breakout strategy")
    parser.add_argument("--symbol", default="ETH-USDT-SWAP")
    parser.add_argument("--bar", default="4H")
    parser.add_argument("--limit", type=int, default=2190)
    parser.add_argument("--output", default="configs/active_strategy.json")
    parser.add_argument("--min-trades", type=int, default=10)
    parser.add_argument("--max-drawdown", type=float, default=15.0)
    parser.add_argument("--risk-fraction", type=float, default=0.25)
    parser.add_argument("--k", type=float, default=0.8)
    parser.add_argument("--trailing-atr", type=float, default=2.5)
    parser.add_argument("--max-hold-bars", type=int, default=12)
    return parser.parse_args(argv)


async def run_backtest(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = VolBreakoutConfig(
        min_trade_count=args.min_trades,
        max_drawdown_percent=args.max_drawdown,
        risk_fraction=args.risk_fraction,
    )
    parameters = VolBreakoutParameters(
        k=args.k,
        trailing_atr=args.trailing_atr,
        max_hold_bars=args.max_hold_bars,
        bar=args.bar,
    )
    client = OkxRestClient(base_url=_OKX_REST_BASE_URL, api_key="")
    try:
        rows = await fetch_okx_history_rows(client, symbol=args.symbol, bar=args.bar, limit=args.limit)
    finally:
        await client.aclose()

    result = evaluate_vol_breakout(parameters, rows, config=config)
    _print_summary(result)
    if not candidate_passes(result, config=config):
        print("vol breakout candidate did not pass selection gates", file=sys.stderr)
        return 2

    snapshot = build_vol_breakout_snapshot(
        selected=result,
        symbol=args.symbol,
        generated_at=datetime.now(UTC),
        config=config,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    tmp_path.replace(output_path)
    print(f"wrote fixed strategy snapshot: {output_path}")
    return 0


def main() -> int:
    return asyncio.run(run_backtest())


def _print_summary(result) -> None:
    p = result.parameters
    print(
        "vol_breakout "
        f"bar={p.bar} k={p.k} trailing_atr={p.trailing_atr} max_hold_bars={p.max_hold_bars} "
        f"trades={result.trade_count} return={result.return_percent:.4f}% "
        f"drawdown={result.max_drawdown_percent:.4f}% win={result.win_rate:.4f} "
        f"pf={result.profit_factor:.4f} stability={result.stability_score:.4f}"
    )


if __name__ == "__main__":
    raise SystemExit(main())

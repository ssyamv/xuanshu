from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from xuanshu.core.enums import RunMode
from xuanshu.infra.okx.rest import OkxRestClient
from xuanshu.momentum.okx_history import fetch_okx_history_rows
from xuanshu.vote_trend.backtest import (
    VoteTrendConfig,
    VoteTrendParameters,
    build_vote_trend_snapshot,
    evaluate_vote_trend,
)

_OKX_REST_BASE_URL = "https://www.okx.com"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest a vote-trend research strategy using OKX history")
    parser.add_argument("--symbol", default="BTC-USDT-SWAP")
    parser.add_argument("--bar", default="12H")
    parser.add_argument("--limit", type=int, default=4380)
    parser.add_argument("--output", default=None)
    parser.add_argument("--activate-normal", action="store_true")
    parser.add_argument("--initial-equity", type=float, default=1000.0)
    parser.add_argument("--initial-available-balance", type=float, default=None)
    parser.add_argument("--fast-ema-period", type=int, default=20)
    parser.add_argument("--slow-ema-period", type=int, default=200)
    parser.add_argument("--lookback-bars", type=int, default=6)
    parser.add_argument("--channel-bars", type=int, default=24)
    parser.add_argument("--threshold-bps", type=int, default=0)
    parser.add_argument("--required-votes", type=int, default=4)
    parser.add_argument("--stop-loss-bps", type=int, default=75)
    parser.add_argument("--take-profit-bps", type=int, default=2400)
    parser.add_argument("--max-hold-bars", type=int, default=36)
    parser.add_argument("--long-only", action="store_true")
    return parser.parse_args(argv)


async def run_backtest(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    parameters = VoteTrendParameters(
        fast_ema_period=args.fast_ema_period,
        slow_ema_period=args.slow_ema_period,
        lookback_bars=args.lookback_bars,
        channel_bars=args.channel_bars,
        threshold_bps=args.threshold_bps,
        required_votes=args.required_votes,
        stop_loss_bps=args.stop_loss_bps,
        take_profit_bps=args.take_profit_bps,
        max_hold_bars=args.max_hold_bars,
        allow_short=not args.long_only,
    )
    config = VoteTrendConfig(
        symbol=args.symbol,
        initial_equity=args.initial_equity,
        initial_available_balance=args.initial_available_balance,
    )
    client = OkxRestClient(base_url=_OKX_REST_BASE_URL, api_key="")
    try:
        rows = await fetch_okx_history_rows(client, symbol=args.symbol, bar=args.bar, limit=args.limit)
    finally:
        await client.aclose()
    result = evaluate_vote_trend(parameters, rows, config=config)
    _print_summary(result=result, symbol=args.symbol, bar=args.bar)
    if args.output:
        snapshot = build_vote_trend_snapshot(
            selected=result,
            symbol=args.symbol,
            bar=args.bar,
            generated_at=datetime.now(UTC),
            config=config,
        )
        if args.activate_normal:
            snapshot = snapshot.model_copy(update={"market_mode": RunMode.NORMAL})
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        tmp_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        tmp_path.replace(output_path)
        print(f"wrote fixed strategy snapshot: {output_path}")
    return 0


def main() -> int:
    return asyncio.run(run_backtest())


def _print_summary(*, result, symbol: str, bar: str) -> None:
    p = result.parameters
    mode = "both" if p.allow_short else "long_only"
    print(
        "vote_trend "
        f"symbol={symbol} bar={bar} mode={mode} fast={p.fast_ema_period} slow={p.slow_ema_period} "
        f"lookback={p.lookback_bars} channel={p.channel_bars} threshold_bps={p.threshold_bps} "
        f"votes={p.required_votes} sl={p.stop_loss_bps} tp={p.take_profit_bps} hold={p.max_hold_bars} "
        f"trades={result.trade_count} long={result.long_trade_count} short={result.short_trade_count} "
        f"return={result.return_percent:.4f}% drawdown={result.max_drawdown_percent:.4f}% "
        f"win={result.win_rate:.4f} pf={result.profit_factor:.4f} "
        f"equity={result.initial_equity:.2f}->{result.final_equity:.2f} blocked={result.blocked_signal_count}"
    )


if __name__ == "__main__":
    raise SystemExit(main())

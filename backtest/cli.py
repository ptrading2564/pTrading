"""CLI runner for AI-autonomous backtesting, parameter sweeps, and walk-forward validation.

Examples:
  python -m backtest.cli --mode screener --start 2025-01-01 --end 2025-12-31 --feed sip
  python -m backtest.cli --mode fixed --start 2025-01-01 --end 2025-12-31 --feed iex
  python -m backtest.cli --mode fixed --start 2025-01-01 --end 2025-12-31 --sweep-be 0.75,1.0,1.25,1.5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import List

from backtest.config import BacktestConfig
from backtest.engine import BacktestEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="pTrading Automated Portfolio Backtester CLI")
    p.add_argument("--name", default="", help="Optional display name for the run")
    p.add_argument("--mode", choices=["screener", "fixed", "chartmill_history"], default="screener")
    p.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    p.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    p.add_argument("--feed", choices=["sip", "iex"], default="sip")
    p.add_argument("--capital", type=float, default=50000.0)
    p.add_argument("--risk-profile", choices=["P1_DEFENSIVE", "P2_BALANCED", "P3_GROWTH", "CUSTOM"], default="P2_BALANCED")
    p.add_argument("--risk-pct", type=float, default=0.5)
    p.add_argument("--basket-size", type=int, default=38, help="Max basket size (0 = uncapped)")
    p.add_argument("--symbols", default="", help="Comma-separated custom symbols for fixed mode")
    p.add_argument("--min-rvol", type=float, default=1.5)
    p.add_argument("--max-atr-ext", type=float, default=0.75)
    p.add_argument("--be-trigger-r", type=float, default=1.0)
    p.add_argument("--tp-r", type=float, default=2.0)
    p.add_argument("--slippage-bps", type=float, default=3.0)
    p.add_argument("--no-slot-recycling", action="store_true")
    p.add_argument("--no-health-ladder", action="store_true")
    p.add_argument("--no-fomc-guard", action="store_true")
    p.add_argument("--no-spy-qqq-gate", action="store_true")
    p.add_argument("--unlock-on-reject", action="store_true")
    p.add_argument("--edgar-fundamentals", action="store_true")
    p.add_argument("--entry-cutoff", default="", help="Optional custom ET entry cutoff, e.g. 11:30 or 12:00")
    p.add_argument("--midday-blackout", default="", help="Optional lunch blackout window, e.g. 12:00-14:00")
    p.add_argument("--spy-ema20", action="store_true", help="Require SPY prior-day close >= daily EMA20")
    p.add_argument("--no-1m", action="store_true", help="Use 5m bars only for faster sweeps")
    p.add_argument("--no-persist", action="store_true", help="Do not save run to data/backtest.db")
    p.add_argument("--sweep-be", default="", help="Optional comma-separated breakeven_trigger_r sweep values")
    p.add_argument("--json", action="store_true", help="Print compact JSON summary")
    return p


async def _run_async(args: argparse.Namespace) -> int:
    be_values: List[float] = (
        [float(x.strip()) for x in args.sweep_be.split(",") if x.strip()]
        if args.sweep_be
        else [args.be_trigger_r]
    )

    custom_syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else []
    summaries = []

    for be_val in be_values:
        run_name = args.name or (
            f"CLI {args.mode.upper()} ({args.feed.upper()}) BE={be_val}R {args.start}..{args.end}"
        )
        cfg = BacktestConfig(
            name=run_name,
            mode=args.mode,
            start_date=args.start,
            end_date=args.end,
            feed=args.feed,
            initial_capital=args.capital,
            risk_profile=args.risk_profile,
            risk_percent_per_trade=args.risk_pct,
            max_basket_size=(args.basket_size if args.basket_size > 0 else None),
            custom_symbols=custom_syms,
            scanner_min_rvol=args.min_rvol,
            scanner_max_atr_extension=args.max_atr_ext,
            breakeven_trigger_r=be_val,
            take_profit_r=args.tp_r,
            exit_slippage_bps=args.slippage_bps,
            enable_slot_recycling=not args.no_slot_recycling,
            enable_health_ladder=not args.no_health_ladder,
            enable_fomc_guard=not args.no_fomc_guard,
            enable_spy_qqq_gate=not args.no_spy_qqq_gate,
            lock_triggered_on_reject=not args.unlock_on_reject,
            enable_edgar_fundamentals=args.edgar_fundamentals,
            custom_entry_cutoff_et=args.entry_cutoff.strip() or None,
            midday_blackout_start_et=(args.midday_blackout.split("-")[0].strip() if "-" in args.midday_blackout else None),
            midday_blackout_end_et=(args.midday_blackout.split("-")[1].strip() if "-" in args.midday_blackout else None),
            require_spy_above_daily_ema20=args.spy_ema20,
            use_1m_execution=not args.no_1m,
        )

        engine = BacktestEngine(cfg)
        result = await engine.run(persist=not args.no_persist)
        summaries.append({"run_id": result["run_id"], "name": result["name"], "summary": result["summary"]})

        if not args.json:
            s = result["summary"]
            print(f"\n=== {result['name']} (run_id={result['run_id']}) ===")
            print(
                f"Ending Equity: ${s['ending_equity']:,.2f} | Return: {s['total_return_pct']:+.2f}% "
                f"(SPY: {s['spy_total_return_pct']:+.2f}%, Alpha: {s['alpha_vs_spy_pct']:+.2f}%)"
            )
            print(
                f"CAGR: {s['cagr_pct']:+.2f}% | Sharpe: {s['sharpe_ratio']:.2f} | "
                f"Max DD: -{s['max_drawdown_pct']:.2f}% | Health: {s['health_score']}/100 ({s['health_grade']})"
            )
            print(
                f"Trades: {s['total_trades']} | WR: {s['win_rate_pct']:.1f}% | "
                f"R:R: {s['realized_rr_r']:.2f}:1 | Exp: {s['expectancy_r']:+.3f}R | PF: {s['profit_factor']:.2f}"
            )

    if args.json:
        print(json.dumps(summaries, indent=2))
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    sys.exit(main())

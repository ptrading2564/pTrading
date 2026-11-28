"""Bridge feeding backtest outputs directly into live AnalyticsService and StrategyReviewer (read-only),
plus deep-dive failure diagnostics (exit breakdown, R/MFE histograms, day-of-week, symbol leaderboard).
"""

from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any, Dict, List, Tuple

from app.services.analytics_service import (
    analytics_service,
    calculate_analytical_ror,
    calculate_strategy_health_score,
    run_monte_carlo_and_projections,
)
from app.services.strategy_reviewer import strategy_reviewer
from backtest.config import BacktestConfig


def compute_annualized_metrics(
    daily_rows: List[Dict[str, Any]],
    initial_capital: float,
) -> Dict[str, float]:
    """Computes CAGR, Sharpe, Sortino, Calmar, and SPY Buy-and-Hold comparison."""
    if not daily_rows or initial_capital <= 0:
        return {
            "total_return_pct": 0.0,
            "cagr_pct": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "calmar_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "spy_total_return_pct": 0.0,
            "alpha_vs_spy_pct": 0.0,
            "trading_days": 0,
        }

    end_eq = float(daily_rows[-1]["ending_equity"])
    total_ret_pct = ((end_eq - initial_capital) / initial_capital) * 100.0
    n_days = len(daily_rows)
    years = max(n_days / 252.0, 1.0 / 252.0)

    if end_eq > 0 and initial_capital > 0:
        cagr_pct = ((end_eq / initial_capital) ** (1.0 / years) - 1.0) * 100.0
    else:
        cagr_pct = -100.0

    daily_rets = [float(d["daily_roi_pct"]) / 100.0 for d in daily_rows]
    mean_r = statistics.mean(daily_rets) if daily_rets else 0.0
    std_r = statistics.stdev(daily_rets) if len(daily_rets) >= 2 else 0.0
    downside = [r for r in daily_rets if r < 0]
    down_std = math.sqrt(sum(r * r for r in downside) / len(daily_rets)) if downside else 0.0

    sharpe = ((mean_r / std_r) * math.sqrt(252.0)) if std_r > 1e-12 else 0.0
    sortino = ((mean_r / down_std) * math.sqrt(252.0)) if down_std > 1e-12 else 0.0
    max_dd = max((float(d.get("drawdown_pct") or 0.0) for d in daily_rows), default=0.0)
    calmar = (cagr_pct / max_dd) if max_dd > 0.01 else 0.0

    spy_growth = 1.0
    has_spy = False
    for d in daily_rows:
        sr = d.get("spy_return_pct")
        if sr is not None:
            spy_growth *= (1.0 + float(sr) / 100.0)
            has_spy = True
    spy_total_pct = ((spy_growth - 1.0) * 100.0) if has_spy else 0.0

    return {
        "total_return_pct": round(total_ret_pct, 2),
        "cagr_pct": round(cagr_pct, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "calmar_ratio": round(calmar, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "spy_total_return_pct": round(spy_total_pct, 2),
        "alpha_vs_spy_pct": round(total_ret_pct - spy_total_pct, 2),
        "trading_days": n_days,
    }


def compute_monthly_returns(daily_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_month: Dict[str, List[Dict[str, Any]]] = {}
    for d in daily_rows:
        ym = str(d["date"])[:7]
        by_month.setdefault(ym, []).append(d)

    out: List[Dict[str, Any]] = []
    for ym in sorted(by_month.keys()):
        rows = by_month[ym]
        start_eq = float(rows[0]["starting_equity"])
        end_eq = float(rows[-1]["ending_equity"])
        pnl = round(end_eq - start_eq, 2)
        roi_pct = round(((end_eq - start_eq) / start_eq) * 100.0, 2) if start_eq > 0 else 0.0
        trades = sum(int(r.get("trade_count", 0)) for r in rows)
        out.append({"month": ym, "pnl": pnl, "roi_pct": roi_pct, "trades": trades})
    return out


def compute_regime_breakdown(
    daily_rows: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    regime_by_date = {str(d["date"]): str(d.get("market_regime") or "UNKNOWN") for d in daily_rows}
    days_by_regime: Dict[str, int] = {}
    for reg in regime_by_date.values():
        days_by_regime[reg] = days_by_regime.get(reg, 0) + 1

    trades_by_regime: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        reg = regime_by_date.get(str(t.get("trade_date")), "UNKNOWN")
        trades_by_regime.setdefault(reg, []).append(t)

    out: Dict[str, Dict[str, Any]] = {}
    for reg, n_days in sorted(days_by_regime.items()):
        r_trades = trades_by_regime.get(reg, [])
        rs = [float(t["pnl_r"]) for t in r_trades if t.get("pnl_r") is not None]
        pnl = round(sum(float(t.get("pnl_dollars") or 0.0) for t in r_trades), 2)
        wins = sum(1 for r in rs if r > 0.1)
        out[reg] = {
            "days": n_days,
            "trades": len(r_trades),
            "win_rate_pct": round((wins / len(rs)) * 100.0, 1) if rs else 0.0,
            "avg_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
            "total_r": round(sum(rs), 2) if rs else 0.0,
            "net_pnl": pnl,
        }
    return out


def compute_failure_diagnostics(
    trades: List[Dict[str, Any]],
    daily_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Deep-dive attribution buckets pinpointing where the strategy gains or leaks edge."""
    total_n = len(trades)

    # 1. Exit Reason Breakdown
    by_exit: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        reason = str(t.get("exit_reason") or "UNKNOWN")
        by_exit.setdefault(reason, []).append(t)

    exit_breakdown: List[Dict[str, Any]] = []
    for reason in sorted(by_exit.keys(), key=lambda k: len(by_exit[k]), reverse=True):
        grp = by_exit[reason]
        rs = [float(x["pnl_r"]) for x in grp if x.get("pnl_r") is not None]
        pnl = round(sum(float(x.get("pnl_dollars") or 0.0) for x in grp), 2)
        exit_breakdown.append(
            {
                "reason": reason,
                "count": len(grp),
                "pct": round((len(grp) / total_n) * 100.0, 1) if total_n else 0.0,
                "avg_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
                "total_r": round(sum(rs), 2) if rs else 0.0,
                "net_pnl": pnl,
            }
        )

    # 2. R-Multiple Distribution Histogram
    r_bins = [
        ("<= -1.0R", lambda r: r <= -0.95),
        ("-0.95 to -0.2R", lambda r: -0.95 < r < -0.20),
        ("BE (-0.2 to +0.2R)", lambda r: -0.20 <= r <= 0.20),
        ("+0.2 to +1.0R", lambda r: 0.20 < r < 1.00),
        ("+1.0 to +1.9R", lambda r: 1.00 <= r < 1.90),
        (">= +1.9R (TP)", lambda r: r >= 1.90),
    ]
    r_vals = [float(t["pnl_r"]) for t in trades if t.get("pnl_r") is not None]
    r_distribution = []
    for label, pred in r_bins:
        cnt = sum(1 for r in r_vals if pred(r))
        r_distribution.append(
            {
                "label": label,
                "count": cnt,
                "pct": round((cnt / len(r_vals)) * 100.0, 1) if r_vals else 0.0,
            }
        )

    # 3. MFE Peak Distribution (pinpoints stalling before +2.0R)
    mfe_bins = [
        ("< 0.5R", lambda m: m < 0.5),
        ("0.5 - 1.0R", lambda m: 0.5 <= m < 1.0),
        ("1.0 - 1.5R", lambda m: 1.0 <= m < 1.5),
        ("1.5 - 2.0R", lambda m: 1.5 <= m < 2.0),
        (">= 2.0R", lambda m: m >= 2.0),
    ]
    mfe_vals = [float(t["mfe_r"]) for t in trades if t.get("mfe_r") is not None]
    mfe_distribution = []
    for label, pred in mfe_bins:
        cnt = sum(1 for m in mfe_vals if pred(m))
        mfe_distribution.append(
            {
                "label": label,
                "count": cnt,
                "pct": round((cnt / len(mfe_vals)) * 100.0, 1) if mfe_vals else 0.0,
            }
        )

    # 4. Day-of-Week Breakdown
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    by_dow: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(5)}
    for t in trades:
        td = t.get("trade_date")
        if td:
            try:
                wd = date.fromisoformat(str(td)[:10]).weekday()
                if wd in by_dow:
                    by_dow[wd].append(t)
            except ValueError:
                pass

    day_of_week: List[Dict[str, Any]] = []
    for wd in range(5):
        grp = by_dow[wd]
        rs = [float(x["pnl_r"]) for x in grp if x.get("pnl_r") is not None]
        wins = sum(1 for r in rs if r > 0.1)
        pnl = round(sum(float(x.get("pnl_dollars") or 0.0) for x in grp), 2)
        day_of_week.append(
            {
                "day": dow_names[wd],
                "trades": len(grp),
                "win_rate_pct": round((wins / len(rs)) * 100.0, 1) if rs else 0.0,
                "avg_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
                "total_r": round(sum(rs), 2) if rs else 0.0,
                "net_pnl": pnl,
            }
        )

    # 5. Health-Ladder State Breakdown (days spent in each governance state)
    health_days: Dict[str, int] = {}
    for d in daily_rows:
        st = str(d.get("health_state") or "HEALTHY")
        health_days[st] = health_days.get(st, 0) + 1

    # 6. Top 10 Best & Top 10 Worst Drag Symbols
    by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        sym = str(t.get("symbol") or "").upper()
        if sym:
            by_sym.setdefault(sym, []).append(t)

    sym_stats = []
    for sym, grp in by_sym.items():
        rs = [float(x["pnl_r"]) for x in grp if x.get("pnl_r") is not None]
        wins = sum(1 for r in rs if r > 0.1)
        pnl = round(sum(float(x.get("pnl_dollars") or 0.0) for x in grp), 2)
        sym_stats.append(
            {
                "symbol": sym,
                "trades": len(grp),
                "win_rate_pct": round((wins / len(rs)) * 100.0, 1) if rs else 0.0,
                "total_r": round(sum(rs), 2) if rs else 0.0,
                "net_pnl": pnl,
            }
        )

    top_winners = sorted([s for s in sym_stats if s["total_r"] > 0], key=lambda x: x["total_r"], reverse=True)[:10]
    top_losers = sorted([s for s in sym_stats if s["total_r"] < 0], key=lambda x: x["total_r"])[:10]

    return {
        "exit_reason_breakdown": exit_breakdown,
        "r_distribution": r_distribution,
        "mfe_distribution": mfe_distribution,
        "day_of_week": day_of_week,
        "health_state_days": health_days,
        "top_winners": top_winners,
        "top_losers": top_losers,
    }


async def build_backtest_report_and_review(
    cfg: BacktestConfig,
    trades: List[Dict[str, Any]],
    daily_rows: List[Dict[str, Any]],
    funnel_stats: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], str, str, List[Dict[str, Any]]]:
    """Builds full institutional backtest report reusing live AnalyticsService & StrategyReviewer."""
    cum_metrics = analytics_service.compute_trade_metrics(trades)
    ann = compute_annualized_metrics(daily_rows, cfg.initial_capital)
    monthly = compute_monthly_returns(daily_rows)
    regimes = compute_regime_breakdown(daily_rows, trades)
    deep_diag = compute_failure_diagnostics(trades, daily_rows)

    end_eq = float(daily_rows[-1]["ending_equity"]) if daily_rows else cfg.initial_capital
    last_day = daily_rows[-1] if daily_rows else None
    equity_curve = []
    spy_eq = cfg.initial_capital
    for d in daily_rows:
        sr = d.get("spy_return_pct")
        if sr is not None:
            spy_eq = round(spy_eq * (1.0 + float(sr) / 100.0), 2)
        equity_curve.append(
            {
                "date": d["date"],
                "equity": round(float(d["ending_equity"]), 2),
                "spy_equity": spy_eq,
                "drawdown_pct": round(float(d["drawdown_pct"]), 2),
                "health_state": d.get("health_state", "HEALTHY"),
            }
        )

    roi_equity = {
        "daily_starting_equity": float(last_day["starting_equity"]) if last_day else cfg.initial_capital,
        "daily_ending_equity": end_eq,
        "current_equity": end_eq,
        "initial_capital": cfg.initial_capital,
        "daily_pnl": float(last_day["daily_pnl"]) if last_day else 0.0,
        "daily_roi_pct": float(last_day["daily_roi_pct"]) if last_day else 0.0,
        "cumulative_roi_pct": ann["total_return_pct"],
        "mtd_roi_pct": monthly[-1]["roi_pct"] if monthly else 0.0,
        "max_drawdown_pct": ann["max_drawdown_pct"],
        "trading_days": ann["trading_days"],
        "circuit_breaker_tripped_today": False,
        "market_regime": str(last_day.get("market_regime") or "MIXED") if last_day else "UNKNOWN",
        "daily_spy_return_pct": ann["spy_total_return_pct"],
        "daily_alpha_vs_spy_pct": ann["alpha_vs_spy_pct"],
        "daily_qqq_return_pct": None,
        "equity_curve": equity_curve,
    }

    base_risk_pct = cfg.resolved_base_risk_pct()
    analytical_ror = calculate_analytical_ror(
        win_rate=cum_metrics["win_rate_pct"] / 100.0,
        loss_rate=cum_metrics["loss_rate_pct"] / 100.0,
        payoff_ratio=cum_metrics["realized_rr_r"],
        risk_per_trade_pct=base_risk_pct,
    )

    r_series = [float(t["pnl_r"]) for t in trades if t.get("pnl_r") is not None]
    avg_tpd = (len(trades) / max(1, ann["trading_days"])) if trades else 0.0
    mc_proj = run_monte_carlo_and_projections(
        r_multiples=r_series,
        current_equity=cfg.initial_capital,
        risk_per_trade_pct=base_risk_pct,
        avg_trades_per_day=max(avg_tpd, 0.25),
        num_simulations=cfg.monte_carlo_simulations,
    )

    health_score = calculate_strategy_health_score(
        cum_metrics=cum_metrics,
        roi_equity=roi_equity,
        analytical_ror=analytical_ror,
        mc_ror=mc_proj["empirical_ror"],
    )

    report = {
        "report_date": f"{cfg.start_date} -> {cfg.end_date}",
        "mode": cfg.mode,
        "feed": cfg.feed,
        "health_score": health_score,
        "annualized": ann,
        "monthly_returns": monthly,
        "regime_breakdown": regimes,
        "deep_diagnostics": deep_diag,
        "strategy_params": {
            "risk_percent_per_trade": base_risk_pct,
            "max_concurrent_positions": cfg.max_concurrent_positions,
            "max_total_positions_with_breakeven": cfg.max_total_positions_with_breakeven,
            "daily_max_loss_percent": cfg.daily_max_loss_percent,
            "breakeven_trigger_r": cfg.breakeven_trigger_r,
            "take_profit_r": cfg.take_profit_r,
            "scanner_min_rvol": cfg.scanner_min_rvol,
            "scanner_max_atr_extension": cfg.scanner_max_atr_extension,
            "stale_order_timeout_seconds": cfg.stale_order_timeout_minutes * 60,
        },
        "roi_equity": roi_equity,
        "daily_trades_metrics": cum_metrics,
        "cumulative_trades_metrics": cum_metrics,
        "signal_funnel": funnel_stats,
        "missed_signals_audit": {},
        "risk_of_ruin": {
            "analytical": analytical_ror,
            "monte_carlo": mc_proj["empirical_ror"],
            "sample_size": mc_proj["sample_size"],
            "confidence": mc_proj["confidence"],
        },
        "projections": {
            "base_equity": cfg.initial_capital,
            "avg_trades_per_day": round(avg_tpd, 2),
            "sample_size": mc_proj["sample_size"],
            "confidence": mc_proj["confidence"],
            "horizons": mc_proj["horizons"],
        },
    }

    diagnostics = strategy_reviewer.run_diagnostics(report)
    if cfg.run_gemini_review:
        review_md, review_source, diagnostics = await strategy_reviewer.generate_review(report)
    else:
        review_md = strategy_reviewer.build_deterministic_review(report, diagnostics)
        review_source = "deterministic"

    summary = {
        "ending_equity": round(end_eq, 2),
        "net_pnl": round(end_eq - cfg.initial_capital, 2),
        "total_return_pct": ann["total_return_pct"],
        "cagr_pct": ann["cagr_pct"],
        "sharpe_ratio": ann["sharpe_ratio"],
        "sortino_ratio": ann["sortino_ratio"],
        "max_drawdown_pct": ann["max_drawdown_pct"],
        "spy_total_return_pct": ann["spy_total_return_pct"],
        "alpha_vs_spy_pct": ann["alpha_vs_spy_pct"],
        "total_trades": cum_metrics["total_trades"],
        "win_rate_pct": cum_metrics["win_rate_pct"],
        "realized_rr_r": cum_metrics["realized_rr_r"],
        "expectancy_r": cum_metrics["expectancy_r"],
        "profit_factor": cum_metrics["profit_factor"],
        "sqn": cum_metrics["sqn"],
        "total_r": cum_metrics["total_r"],
        "health_score": health_score.get("score", 0),
        "health_grade": health_score.get("grade", "N/A"),
        "ror_20_analytical": analytical_ror.get("drawdown_probabilities", {}).get("20pct"),
        "ror_20_mc": mc_proj["empirical_ror"].get("20pct"),
        "trading_days": ann["trading_days"],
        "expired_orders": funnel_stats.get("expired_stale_orders", 0),
        "circuit_breaker_days": funnel_stats.get("circuit_breaker_days", 0),
    }

    return summary, report, review_md, review_source, diagnostics

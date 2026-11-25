"""Two-Layer Daily Deep-Dive Strategy Reviewer (Deterministic Diagnostics + Written Review / Optional Gemini)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Tuple
import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class StrategyReviewer:
    """Evaluates daily & rolling trading diagnostics and produces actionable written strategy reviews."""

    @staticmethod
    def run_diagnostics(report: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Layer 1: Runs deterministic diagnostic audits against today's session and rolling baseline."""
        findings: List[Dict[str, Any]] = []

        daily = report.get("daily_trades_metrics", {})
        cum = report.get("cumulative_trades_metrics", {})
        roi = report.get("roi_equity", {})
        ror = report.get("risk_of_ruin", {})
        funnel = report.get("signal_funnel", {})
        params = report.get("strategy_params", {})

        total_cum = int(cum.get("total_trades", 0))
        confidence = "MEANINGFUL" if total_cum >= 30 else ("LOW" if total_cum >= 10 else "INSUFFICIENT")

        # 1. Breakeven Choke Audit
        be = cum.get("breakeven_audit", {})
        stopped_be = int(be.get("stopped_at_be", 0))
        choked = int(be.get("choked_winners_count", 0))
        choked_pct = float(be.get("choked_winners_pct", 0.0))
        if stopped_be >= 3 and choked_pct >= 40.0:
            findings.append(
                {
                    "id": "BREAKEVEN_CHOKE",
                    "title": "Breakeven Stop May Be Choking Winners Early",
                    "severity": "ACTION" if stopped_be >= 10 else "WATCH",
                    "category": "EXIT_MANAGEMENT",
                    "sample_size": stopped_be,
                    "confidence": "MEANINGFUL" if stopped_be >= 15 else "LOW",
                    "evidence": (
                        f"{choked} of {stopped_be} breakeven exits ({choked_pct:.1f}%) later reached +2.0R "
                        f"in the same session."
                    ),
                    "recommendation": (
                        f"Test raising BREAKEVEN_TRIGGER_R from {params.get('breakeven_trigger_r', 1.0)}R "
                        f"to 1.25R-1.5R in paper trading to give breakouts breathing room."
                    ),
                    "config_knob": "BREAKEVEN_TRIGGER_R",
                }
            )

        # 2. Target & MFE Calibration
        mfe_mae = cum.get("mfe_mae", {})
        mfe_samples = int(mfe_mae.get("sample_count", 0))
        avg_mfe = mfe_mae.get("avg_mfe_r")
        avg_winner_mfe = mfe_mae.get("avg_winner_mfe_r")
        if mfe_samples >= 5 and avg_mfe is not None:
            if avg_winner_mfe is not None and avg_winner_mfe >= 2.6:
                findings.append(
                    {
                        "id": "MFE_RUNNERS_CAPPED",
                        "title": "Strong Runners Capped at +2.0R Fixed Target",
                        "severity": "WATCH",
                        "category": "EXIT_MANAGEMENT",
                        "sample_size": mfe_samples,
                        "confidence": confidence,
                        "evidence": (
                            f"Winning trades average +{avg_winner_mfe:.2f}R MFE while exits are hard-capped at +2.0R."
                        ),
                        "recommendation": (
                            "Consider testing a partial scale-out (e.g., 50% at +2.0R) with a trailing stop "
                            "on the remaining runner once sample size exceeds 30 trades."
                        ),
                        "config_knob": "TAKE_PROFIT_R / TRAILING_STOP",
                    }
                )
            elif avg_mfe < 1.35 and float(cum.get("expectancy_r", 0.0)) < 0.15:
                findings.append(
                    {
                        "id": "MFE_STALLING_EARLY",
                        "title": "Breakouts Stalling Before +2.0R Target",
                        "severity": "ACTION" if mfe_samples >= 15 else "WATCH",
                        "category": "EXIT_MANAGEMENT",
                        "sample_size": mfe_samples,
                        "confidence": confidence,
                        "evidence": (
                            f"Average trade MFE is only +{avg_mfe:.2f}R with cumulative expectancy {cum.get('expectancy_r', 0.0):+.2f}R."
                        ),
                        "recommendation": (
                            "Evaluate tightening entry filters (higher SCANNER_MIN_RVOL or lower SCANNER_MAX_ATR_EXTENSION) "
                            "or testing a +1.5R partial profit target."
                        ),
                        "config_knob": "SCANNER_MIN_RVOL",
                    }
                )

        # 3. Time-of-Day Drag (Lunch Chop)
        tod = cum.get("time_of_day_buckets", {})
        lunch = tod.get("12:00-14:00")
        morning = tod.get("09:45-10:30")
        if lunch and int(lunch["trades"]) >= 4 and float(lunch["avg_r"]) < -0.15:
            findings.append(
                {
                    "id": "LUNCH_HOUR_DRAG",
                    "title": "Midday (12:00-14:00 ET) Entries Dragging Performance",
                    "severity": "ACTION" if int(lunch["trades"]) >= 12 else "WATCH",
                    "category": "TIMING",
                    "sample_size": int(lunch["trades"]),
                    "confidence": "MEANINGFUL" if int(lunch["trades"]) >= 15 else "LOW",
                    "evidence": (
                        f"12:00-14:00 ET entries average {lunch['avg_r']:+.2f}R ({lunch['win_rate_pct']:.1f}% WR across "
                        f"{lunch['trades']} trades)"
                        + (
                            f" vs. {morning['avg_r']:+.2f}R in 09:45-10:30 ET."
                            if morning and morning.get("trades")
                            else "."
                        )
                    ),
                    "recommendation": (
                        "Consider adding a midday blackout window (12:00-14:00 ET) or tightening the entry cutoff "
                        "to morning momentum hours."
                    ),
                    "config_knob": "ENTRY_WINDOW_CUTOFF",
                }
            )

        # 4. Stop-Placement Audit (OR_LOW vs ATR_1_5)
        stop_buckets = cum.get("stop_type_buckets", {})
        or_b = stop_buckets.get("OR_LOW")
        atr_b = stop_buckets.get("ATR_1_5")
        if or_b and atr_b and int(or_b["trades"]) >= 5 and int(atr_b["trades"]) >= 5:
            diff = float(or_b["avg_r"]) - float(atr_b["avg_r"])
            if abs(diff) >= 0.35:
                better = "OR_LOW" if diff > 0 else "ATR_1_5"
                worse = "ATR_1_5" if diff > 0 else "OR_LOW"
                findings.append(
                    {
                        "id": "STOP_TYPE_DIVERGENCE",
                        "title": f"{better} Stops Significantly Outperforming {worse}",
                        "severity": "WATCH",
                        "category": "RISK_PLACEMENT",
                        "sample_size": int(or_b["trades"]) + int(atr_b["trades"]),
                        "confidence": confidence,
                        "evidence": (
                            f"OR_LOW avg {or_b['avg_r']:+.2f}R ({or_b['trades']} trades) vs. "
                            f"ATR_1_5 avg {atr_b['avg_r']:+.2f}R ({atr_b['trades']} trades)."
                        ),
                        "recommendation": (
                            f"Review stop-distance calibration; {worse} stops may be getting clipped by routine noise."
                        ),
                        "config_knob": "STOP_LOSS_RULE",
                    }
                )

        # 5. Execution Slippage & Stale Order Audit
        slip = cum.get("slippage", {})
        avg_slip_r = float(slip.get("avg_slippage_r", 0.0))
        if int(slip.get("sample_count", 0)) >= 3 and avg_slip_r >= 0.10:
            findings.append(
                {
                    "id": "SLIPPAGE_LEAK",
                    "title": "Entry Slippage Eroding Realized R:R",
                    "severity": "ACTION",
                    "category": "EXECUTION",
                    "sample_size": int(slip["sample_count"]),
                    "confidence": "MEANINGFUL" if int(slip["sample_count"]) >= 10 else "LOW",
                    "evidence": (
                        f"Average entry slippage is {avg_slip_r:.2f}R (${slip.get('avg_slippage_dollars', 0.0):.2f}/trade, "
                        f"total ${slip.get('total_slippage_dollars', 0.0):.2f}), exceeding the 0.10R threshold."
                    ),
                    "recommendation": (
                        "Check spread liquidity in the ChartMill basket, verify ALPACA_DATA_FEED latency, "
                        "or tighten the 0.05% marketable-limit offset."
                    ),
                    "config_knob": "ENTRY_SLIPPAGE_BUFFER",
                }
            )

        expired_today = int(funnel.get("expired_stale_orders", 0))
        if expired_today >= 2:
            findings.append(
                {
                    "id": "STALE_ORDER_EXPIRATIONS",
                    "title": "Multiple Unfilled Entry Orders Expired Today",
                    "severity": "WATCH",
                    "category": "EXECUTION",
                    "sample_size": expired_today,
                    "confidence": "LOW",
                    "evidence": (
                        f"{expired_today} limit entry orders expired unfilled after "
                        f"{params.get('stale_order_timeout_seconds', 180)}s today."
                    ),
                    "recommendation": (
                        "Audit whether expired orders ran away without filling (adverse selection) and consider "
                        "widening the marketable-limit buffer slightly on fast breakouts."
                    ),
                    "config_knob": "STALE_ORDER_TIMEOUT_SECONDS",
                }
            )

        # 6. Cluster Stop-Out / Correlation Risk
        clusters_today = daily.get("cluster_stop_events", [])
        if clusters_today:
            syms = ", ".join(clusters_today[0]["symbols"])
            findings.append(
                {
                    "id": "CLUSTER_STOP_OUT",
                    "title": "Correlated Cluster Stop-Out Detected Today",
                    "severity": "ACTION",
                    "category": "PORTFOLIO_RISK",
                    "sample_size": len(clusters_today[0]["symbols"]),
                    "confidence": "MEANINGFUL",
                    "evidence": (
                        f"{clusters_today[0]['count']} positions ({syms}) stopped out within 15 minutes of each other."
                    ),
                    "recommendation": (
                        "Ensure concurrent entries aren't concentrated in the same sector/theme (e.g., semis) "
                        "so 4 x 0.5% positions don't act like a single 2.0% beta bet."
                    ),
                    "config_knob": "MAX_CONCURRENT_POSITIONS",
                }
            )

        # 7. Risk of Ruin & Kelly Sizing Drift
        analytical = ror.get("analytical", {})
        ror_20 = analytical.get("drawdown_probabilities", {}).get("20pct")
        sizing_status = analytical.get("sizing_status")
        if sizing_status in ("OVER_KELLY", "NEGATIVE_EDGE") and total_cum >= 10:
            findings.append(
                {
                    "id": "SIZING_OR_EDGE_WARNING",
                    "title": (
                        "Negative Cumulative Expectancy — Risk of Ruin Elevated"
                        if sizing_status == "NEGATIVE_EDGE"
                        else "Position Risk Exceeds Full Kelly Sizing"
                    ),
                    "severity": "ACTION",
                    "category": "RISK_OF_RUIN",
                    "sample_size": total_cum,
                    "confidence": confidence,
                    "evidence": (
                        f"Analytical RoR (-20% DD) is {ror_20}% | Current risk: {params.get('risk_percent_per_trade', 0.5)}% "
                        f"vs. Half-Kelly: {analytical.get('half_kelly_pct', 0.0)}%."
                    ),
                    "recommendation": (
                        "Pause AUTO mode or cut RISK_PERCENT_PER_TRADE to 0.25% until rolling 20-trade expectancy turns positive."
                        if sizing_status == "NEGATIVE_EDGE"
                        else "Reduce RISK_PERCENT_PER_TRADE to stay at or below Half-Kelly."
                    ),
                    "config_knob": "RISK_PERCENT_PER_TRADE",
                }
            )

        # 8. Daily Circuit Breaker Tripped
        if roi.get("circuit_breaker_tripped_today"):
            findings.append(
                {
                    "id": "CIRCUIT_BREAKER_TRIPPED",
                    "title": "Daily -2.0% Circuit Breaker Triggered Today",
                    "severity": "ACTION",
                    "category": "PORTFOLIO_RISK",
                    "sample_size": int(daily.get("total_trades", 0)),
                    "confidence": "MEANINGFUL",
                    "evidence": f"Daily ROI hit {roi.get('daily_roi_pct', 0.0):+.2f}% in a {roi.get('market_regime', 'UNKNOWN')} market regime.",
                    "recommendation": "Review today's losing trades before re-enabling AUTO execution tomorrow.",
                    "config_knob": "DAILY_MAX_LOSS_PERCENT",
                }
            )

        # 8b. ChartMill Setup & Fundamental Rating Attribution
        cm_attr = cum.get("chartmill_attribution", {})
        setup_b = cm_attr.get("setup_rating_buckets", {})
        s_hi = setup_b.get("Setup >= 8")
        s_lo = setup_b.get("Setup < 8")
        if s_hi and s_lo and int(s_hi["trades"]) >= 4 and int(s_lo["trades"]) >= 4:
            if float(s_hi["avg_r"]) - float(s_lo["avg_r"]) >= 0.35:
                findings.append(
                    {
                        "id": "CHARTMILL_SETUP_EDGE",
                        "title": "Higher ChartMill Setup Ratings (>= 8) Outperforming",
                        "severity": "WATCH",
                        "category": "SCREENER_QUALITY",
                        "sample_size": int(s_hi["trades"]) + int(s_lo["trades"]),
                        "confidence": confidence,
                        "evidence": (
                            f"Setup >= 8 trades average {s_hi['avg_r']:+.2f}R ({s_hi['win_rate_pct']:.1f}% WR, n={s_hi['trades']}) "
                            f"vs. Setup < 8 at {s_lo['avg_r']:+.2f}R (n={s_lo['trades']})."
                        ),
                        "recommendation": "Consider raising your ChartMill screener Setup Rating filter from >= 7 to >= 8.",
                        "config_knob": "CHARTMILL_SETUP_FILTER",
                    }
                )

        fund_b = cm_attr.get("fundamental_rating_buckets", {})
        f_hi = fund_b.get("Fund >= 5")
        f_lo = fund_b.get("Fund < 5")
        if f_hi and f_lo and int(f_hi["trades"]) >= 4 and int(f_lo["trades"]) >= 4:
            if float(f_hi["avg_r"]) - float(f_lo["avg_r"]) >= 0.35:
                findings.append(
                    {
                        "id": "CHARTMILL_FUNDAMENTAL_EDGE",
                        "title": "Fundamentally Stronger Stocks (Fund >= 5) Outperforming",
                        "severity": "WATCH",
                        "category": "SCREENER_QUALITY",
                        "sample_size": int(f_hi["trades"]) + int(f_lo["trades"]),
                        "confidence": confidence,
                        "evidence": (
                            f"Fund >= 5 trades average {f_hi['avg_r']:+.2f}R (n={f_hi['trades']}) "
                            f"vs. Fund < 5 at {f_lo['avg_r']:+.2f}R (n={f_lo['trades']})."
                        ),
                        "recommendation": "Consider adding Fundamental Rating >= 5 to your ChartMill morning screener.",
                        "config_knob": "CHARTMILL_FUNDAMENTAL_FILTER",
                    }
                )

        sector_b = cm_attr.get("sector_buckets", {})
        weak_sectors = [
            (sec, st) for sec, st in sector_b.items()
            if int(st["trades"]) >= 4 and float(st["avg_r"]) <= -0.35
        ]
        if weak_sectors:
            sec_name, sec_st = min(weak_sectors, key=lambda x: x[1]["avg_r"])
            findings.append(
                {
                    "id": "SECTOR_DRAG",
                    "title": f"Sector Drag Detected in {sec_name}",
                    "severity": "WATCH",
                    "category": "SCREENER_QUALITY",
                    "sample_size": int(sec_st["trades"]),
                    "confidence": "MEANINGFUL" if int(sec_st["trades"]) >= 10 else "LOW",
                    "evidence": (
                        f"{sec_name} breakouts average {sec_st['avg_r']:+.2f}R "
                        f"({sec_st['win_rate_pct']:.1f}% WR across {sec_st['trades']} trades, total {sec_st['total_r']:+.2f}R)."
                    ),
                    "recommendation": f"Review whether {sec_name} stocks are prone to false ORB breakouts in the current regime.",
                    "config_knob": "CHARTMILL_SECTOR_FILTER",
                }
            )

        # 8c. Missed / Rejected Signals Counterfactual Audit
        missed = report.get("missed_signals_audit", {})
        conc_cnt = int(missed.get("concurrency_blocked_count", 0))
        conc_net_r = float(missed.get("concurrency_net_r", 0.0))
        if conc_cnt >= 3 and int(missed.get("evaluated_count", 0)) >= 3:
            if conc_net_r >= 2.0:
                findings.append(
                    {
                        "id": "CONCURRENCY_OPPORTUNITY_COST",
                        "title": "Concurrency Cap Blocked Net-Winning Signals Today",
                        "severity": "WATCH",
                        "category": "PORTFOLIO_RISK",
                        "sample_size": conc_cnt,
                        "confidence": "LOW",
                        "evidence": (
                            f"{conc_cnt} signals blocked by the 4/4 concurrency cap simulated a net "
                            f"{conc_net_r:+.2f}R ({missed.get('sim_wins', 0)}W / {missed.get('sim_losses', 0)}L / {missed.get('sim_breakevens', 0)}BE)."
                        ),
                        "recommendation": (
                            "Check whether slots were occupied by prior-day carryovers; track across >= 15 sessions before considering raising MAX_CONCURRENT_POSITIONS."
                        ),
                        "config_knob": "MAX_CONCURRENT_POSITIONS",
                    }
                )
            elif conc_net_r <= -1.0:
                findings.append(
                    {
                        "id": "CONCURRENCY_CAP_PROTECTED",
                        "title": "4/4 Concurrency Cap Protected Capital Today",
                        "severity": "INFO",
                        "category": "PORTFOLIO_RISK",
                        "sample_size": conc_cnt,
                        "confidence": "LOW",
                        "evidence": (
                            f"{conc_cnt} signals blocked by the 4/4 concurrency cap would have lost "
                            f"{conc_net_r:+.2f}R in counterfactual simulation."
                        ),
                        "recommendation": "Keep MAX_CONCURRENT_POSITIONS at 4; it shielded you from midday overtrading.",
                        "config_knob": "MAX_CONCURRENT_POSITIONS",
                    }
                )

        # 9. Healthy Baseline Info Note if no issues found
        if not findings:
            findings.append(
                {
                    "id": "SYSTEM_HEALTHY",
                    "title": "No Critical Strategy Leaks Detected",
                    "severity": "INFO",
                    "category": "HEALTH",
                    "sample_size": total_cum,
                    "confidence": confidence,
                    "evidence": (
                        f"Cumulative expectancy is {cum.get('expectancy_r', 0.0):+.2f}R across {total_cum} closed trades "
                        f"with Profit Factor {cum.get('profit_factor', 0.0):.2f}."
                    ),
                    "recommendation": (
                        "Continue collecting trade samples (target >= 30 trades) before tuning any strategy parameters."
                        if total_cum < 30
                        else "Maintain current execution rules and monitor rolling 20-trade expectancy."
                    ),
                    "config_knob": "NONE",
                }
            )

        return findings

    @staticmethod
    def build_deterministic_review(
        report: Dict[str, Any], diagnostics: List[Dict[str, Any]]
    ) -> str:
        """Layer 2 (Default): Builds a structured, evidence-backed Markdown daily strategy review."""
        d_date = report["report_date"]
        roi = report["roi_equity"]
        daily = report["daily_trades_metrics"]
        cum = report["cumulative_trades_metrics"]
        ror = report["risk_of_ruin"]
        proj = report["projections"]
        funnel = report["signal_funnel"]

        analytical = ror.get("analytical", {})
        mc_ror = ror.get("monte_carlo", {})
        ror_20_a = analytical.get("drawdown_probabilities", {}).get("20pct")
        ror_20_mc = mc_ror.get("20pct")

        lines: List[str] = [
            f"# Daily Strategy Review — {d_date}",
            "",
            "## 1. Executive Summary",
            (
                f"- **Session P&L & ROI:** `${roi['daily_pnl']:+,.2f}` (`{roi['daily_roi_pct']:+.2f}%`) "
                f"across **{daily['total_trades']} closed trade(s)** "
                f"({daily['wins']}W / {daily['losses']}L / {daily['breakevens']}BE, Net `{daily['total_r']:+.2f}R`)."
            ),
            (
                f"- **Account Equity:** `${roi['daily_starting_equity']:,.2f}` → `${roi['daily_ending_equity']:,.2f}` "
                f"| **Cumulative ROI:** `{roi['cumulative_roi_pct']:+.2f}%` over {roi['trading_days']} trading day(s)."
            ),
            (
                f"- **Market Regime & Benchmark:** Regime `{roi['market_regime']}`"
                + (
                    f" | SPY `{roi['daily_spy_return_pct']:+.2f}%` (Alpha `{roi['daily_alpha_vs_spy_pct']:+.2f}%`)"
                    if roi.get("daily_spy_return_pct") is not None
                    else " | Benchmark data pending"
                )
                + (
                    f" | QQQ `{roi['daily_qqq_return_pct']:+.2f}%`"
                    if roi.get("daily_qqq_return_pct") is not None
                    else ""
                )
                + "."
            ),
            "",
            "## 2. What Worked Today",
        ]

        if daily["total_trades"] == 0:
            lines.append(
                f"- No closed trades recorded today ({funnel['total_signals']} signal(s) received; "
                f"capital preserved in cash)."
            )
        else:
            top_syms = [s for s in daily.get("symbol_breakdown", []) if s["net_pnl"] > 0][:3]
            if top_syms:
                sym_str = ", ".join(
                    f"**{s['symbol']}** (`${s['net_pnl']:+,.2f}`, `{s['total_r']:+.2f}R`)"
                    for s in top_syms
                )
                lines.append(f"- **Top Performers:** {sym_str}.")
            else:
                lines.append("- No net-winning symbols today; focus on minimizing drawdown and reviewing entry quality.")

            be_today = daily.get("breakeven_audit", {})
            if be_today.get("stepped_count", 0) > 0:
                lines.append(
                    f"- **Risk Protection:** {be_today['stepped_count']} trade(s) reached `+1.0R` and stepped to breakeven "
                    f"({be_today['reached_tp_after_be']} hit `+2.0R` TP, {be_today['stopped_at_be']} exited flat)."
                )

            if daily.get("realized_rr_r", 0) > 0:
                lines.append(
                    f"- **Session R:R & Expectancy:** Realized R:R `{daily['realized_rr_r']:.2f}:1` "
                    f"(Planned `2.00:1`) | Expectancy `{daily['expectancy_r']:+.2f}R` (`${daily['expectancy_dollars']:+,.2f}`/trade)."
                )

        lines.extend(["", "## 3. Friction, Leaks & Diagnostic Findings"])
        for f in diagnostics:
            badge = f"[{f['severity']}]"
            lines.append(
                f"- **{badge} {f['title']}** *(n={f['sample_size']}, confidence: {f['confidence']})*: {f['evidence']}"
            )

        lines.extend(
            [
                "",
                "## 4. Rolling Edge, Risk of Ruin & Forward Trajectory",
                (
                    f"- **Cumulative Edge ({cum['total_trades']} trades):** Win Rate `{cum['win_rate_pct']:.1f}%` | "
                    f"Realized R:R `{cum['realized_rr_r']:.2f}:1` | Expectancy `{cum['expectancy_r']:+.3f}R` | "
                    f"Profit Factor `{cum['profit_factor']:.2f}` | SQN `{cum['sqn']:.2f}`."
                ),
                (
                    f"- **Risk of Ruin (-20% Drawdown):** Analytical `{ror_20_a if ror_20_a is not None else 'N/A'}%` | "
                    f"Monte Carlo `{ror_20_mc if ror_20_mc is not None else 'N/A'}%` | "
                    f"Half-Kelly `{analytical.get('half_kelly_pct', 0.0):.2f}%` vs. Active Risk `{analytical.get('current_risk_pct', 0.5):.2f}%`."
                ),
            ]
        )

        horizons = proj.get("horizons", [])
        if horizons:
            h_1y = horizons[-1]
            h_30 = horizons[0]
            lines.append(
                f"- **Projections ({proj['confidence']} confidence, ~{proj['avg_trades_per_day']:.1f} trades/day):** "
                f"30D Median `${h_30['p50_equity']:,.2f}` (`{h_30['p50_roi_pct']:+.1f}%`) | "
                f"1Y Median `${h_1y['p50_equity']:,.2f}` (`{h_1y['p50_roi_pct']:+.1f}%`, "
                f"Bear P10 `${h_1y['p10_equity']:,.2f}`, Bull P90 `${h_1y['p90_equity']:,.2f}`)."
            )
        else:
            lines.append("- **Projections:** Insufficient closed trades to run Monte Carlo compounding projections.")

        # Section 5: Separated Operational Fixes vs Rolling Strategy Hypotheses
        op_actions = [
            f for f in diagnostics
            if f["severity"] == "ACTION" and f["category"] in ("EXECUTION", "PORTFOLIO_RISK", "RISK_OF_RUIN")
        ]
        strat_hypotheses = [
            f for f in diagnostics
            if f["category"] in ("EXIT_MANAGEMENT", "TIMING", "RISK_PLACEMENT", "SCREENER_QUALITY")
        ]

        lines.extend(["", "## 5. Suggested Next Steps (Anti-Overfitting Gate)"])
        lines.append("### Immediate Operational Fixes")
        if op_actions:
            for item in op_actions:
                lines.append(f"- **{item['config_knob']}**: {item['recommendation']}")
        else:
            lines.append("- No urgent operational or risk-sizing interventions required today.")

        lines.append("")
        lines.append("### Testable Strategy Hypotheses (Validate in Paper Trading Before Live Changes)")
        if strat_hypotheses:
            for item in strat_hypotheses:
                gate = (
                    "Ready to paper-test"
                    if item["sample_size"] >= 15
                    else f"Monitor only — collect more data (currently n={item['sample_size']}, target >= 15)"
                )
                lines.append(f"- **{item['config_knob']}** *({gate})*: {item['recommendation']}")
        else:
            lines.append(
                "- Keep current ORB parameters stable and continue accumulating samples toward 30+ closed trades."
            )

        return "\n".join(lines)

    async def generate_review(
        self, report: Dict[str, Any]
    ) -> Tuple[str, str, List[Dict[str, Any]]]:
        """Runs Layer 1 diagnostics + Layer 2 written review (Gemini if configured, else deterministic)."""
        diagnostics = self.run_diagnostics(report)
        deterministic_md = self.build_deterministic_review(report, diagnostics)

        if not settings.GEMINI_API_KEY:
            return deterministic_md, "deterministic", diagnostics

        # Optional Gemini Quant Reviewer synthesis
        compact_payload = {
            "report_date": report["report_date"],
            "strategy_params": report["strategy_params"],
            "roi_equity": {k: v for k, v in report["roi_equity"].items() if k != "equity_curve"},
            "daily_trades_metrics": report["daily_trades_metrics"],
            "cumulative_trades_metrics": report["cumulative_trades_metrics"],
            "signal_funnel": report["signal_funnel"],
            "missed_signals_audit": report.get("missed_signals_audit", {}),
            "risk_of_ruin": report["risk_of_ruin"],
            "projections": report["projections"],
            "diagnostic_findings": diagnostics,
        }

        prompt = (
            "You are a senior quantitative trading researcher reviewing an automated US Equities "
            "Opening Range Breakout (ORB) intraday trading system called pTrading.\n"
            "Write a concise, rigorous Daily Strategy Review in GitHub-flavored Markdown with these exact sections:\n"
            "1. Executive Summary\n"
            "2. What Worked Today\n"
            "3. Deep-Dive Diagnostics & Strategy Leaks\n"
            "4. Edge Health, Risk of Ruin & Forward Projections\n"
            "5. Prioritized Next Steps (strictly separating Immediate Operational Fixes from "
            "Testable Strategy Hypotheses, and explicitly citing sample sizes so we never overfit to small samples).\n\n"
            "IMPORTANT METRIC DEFINITIONS:\n"
            "- `slippage_dollars` and `slippage_r` are computed as `(actual_buy_fill_price - signal_limit_price)`. "
            "A NEGATIVE value on a BUY order means positive PRICE IMPROVEMENT (filled cheaper than limit), NOT execution drag or friction. "
            "Only POSITIVE values represent adverse slippage drag.\n"
            "- `missed_signals_audit` contains counterfactual 5m-bar simulations of today's rejected signals using exact strategy rules.\n\n"
            "Ground EVERY claim in the exact numbers from the JSON data below. Reference specific "
            "`app/config.py` parameters when suggesting experiments.\n\n"
            f"DATA:\n{json.dumps(compact_payload, indent=2)}"
        )

        headers = {
            "x-goog-api-key": settings.GEMINI_API_KEY.strip(),
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            # Build ordered candidate list: configured model first, plus live-discovered flash models if needed
            candidate_models = [settings.GEMINI_MODEL]

            async def _discover_flash_models() -> List[str]:
                try:
                    m_res = await client.get(
                        "https://generativelanguage.googleapis.com/v1beta/models",
                        headers={"x-goog-api-key": settings.GEMINI_API_KEY.strip()},
                    )
                    if m_res.status_code != 200:
                        return []
                    found = []
                    for m in m_res.json().get("models", []):
                        methods = m.get("supportedGenerationMethods", [])
                        raw_name = str(m.get("name", "")).replace("models/", "")
                        if "generateContent" in methods and "flash" in raw_name.lower() and "tts" not in raw_name.lower() and "image" not in raw_name.lower():
                            found.append(raw_name)
                    # Prefer newest / lite flash variants
                    found.sort(reverse=True)
                    return found[:4]
                except Exception:
                    return []

            tried = set()
            discovered_added = False
            idx = 0
            while idx < len(candidate_models):
                model_name = candidate_models[idx]
                idx += 1
                if model_name in tried:
                    continue
                tried.add(model_name)

                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model_name}:generateContent"
                )
                try:
                    res = await client.post(
                        url,
                        headers=headers,
                        json={"contents": [{"parts": [{"text": prompt}]}]},
                    )
                    if res.status_code == 200:
                        data = res.json()
                        text = (
                            data.get("candidates", [{}])[0]
                            .get("content", {})
                            .get("parts", [{}])[0]
                            .get("text", "")
                            .strip()
                        )
                        if text:
                            return text, f"gemini ({model_name})", diagnostics
                    logger.warning(
                        "Gemini model %s returned status %d (%s).",
                        model_name,
                        res.status_code,
                        res.text[:200],
                    )
                    if not discovered_added:
                        discovered_added = True
                        for dm in await _discover_flash_models():
                            if dm not in tried and dm not in candidate_models:
                                candidate_models.append(dm)
                except Exception as e:
                    logger.warning("Gemini call for %s failed (%s).", model_name, e)
                    if not discovered_added:
                        discovered_added = True
                        for dm in await _discover_flash_models():
                            if dm not in tried and dm not in candidate_models:
                                candidate_models.append(dm)

        return deterministic_md, "deterministic", diagnostics


strategy_reviewer = StrategyReviewer()

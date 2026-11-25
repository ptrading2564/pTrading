"""Comprehensive Daily & Cumulative Trading Analytics, Risk of Ruin, and Monte Carlo Projections Engine."""

from __future__ import annotations

import logging
import math
import random
import statistics
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.config import settings
from app.db import get_connection

logger = logging.getLogger(__name__)


def _percentile(sorted_vals: Sequence[float], pct: float) -> float:
    """Computes linear-interpolated percentile (0-100) from a pre-sorted sequence."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(sorted_vals[int(k)])
    frac = k - lo
    return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def _parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    """Parses SQLite timestamp or ISO-8601 datetime string into a datetime object."""
    if not value:
        return None
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def _time_bucket_et(entry_time_str: Optional[str], tz: ZoneInfo) -> str:
    """Maps a trade's entry timestamp to an ET intraday session bucket."""
    dt = _parse_iso_dt(entry_time_str)
    if not dt:
        return "UNKNOWN"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    et = dt.astimezone(tz)
    hm = (et.hour, et.minute)
    if hm < (10, 30):
        return "09:45-10:30"
    if hm < (12, 0):
        return "10:30-12:00"
    if hm < (14, 0):
        return "12:00-14:00"
    return "14:00-15:00"


def calculate_analytical_ror(
    win_rate: float,
    loss_rate: float,
    payoff_ratio: float,
    risk_per_trade_pct: float = 0.5,
    drawdown_thresholds: Sequence[float] = (10.0, 20.0, 50.0, 100.0),
) -> Dict[str, Any]:
    """Calculates closed-form Asymmetric Payoff Risk of Ruin and Kelly Criterion.

    Solves p * z^b + q * z^(-1) = 1 for z in (0, 1), where p and q are effective
    win/loss probabilities (excluding breakeven ~0R trades) and b is the realized
    payoff ratio (AvgWin / |AvgLoss|).
    """
    decisive = win_rate + loss_rate
    if decisive <= 0 or risk_per_trade_pct <= 0:
        return {
            "effective_win_rate_pct": 0.0,
            "payoff_ratio": round(payoff_ratio, 3),
            "edge_per_decisive_r": 0.0,
            "kelly_pct": 0.0,
            "half_kelly_pct": 0.0,
            "current_risk_pct": risk_per_trade_pct,
            "sizing_status": "INSUFFICIENT_DATA",
            "drawdown_probabilities": {f"{int(d)}pct": None for d in drawdown_thresholds},
        }

    p = win_rate / decisive
    q = loss_rate / decisive
    b = max(payoff_ratio, 0.0)
    edge = (p * b) - q

    kelly_pct = max(0.0, (p - (q / b)) * 100.0) if b > 0 else 0.0
    half_kelly_pct = kelly_pct / 2.0

    if edge <= 0 or b <= 0:
        probs = {f"{int(d)}pct": 100.0 for d in drawdown_thresholds}
        sizing_status = "NEGATIVE_EDGE"
    elif q <= 0:
        probs = {f"{int(d)}pct": 0.0 for d in drawdown_thresholds}
        sizing_status = "CONSERVATIVE"
    else:
        # Bisection for unique root z* in (0, 1) of f(z) = p*z^b + q/z - 1 = 0
        lo, hi = 1e-9, 1.0 - 1e-9
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            val = p * (mid ** b) + (q / mid) - 1.0
            if val > 0:
                lo = mid
            else:
                hi = mid
        z_root = 0.5 * (lo + hi)

        probs = {}
        f_frac = risk_per_trade_pct / 100.0
        for d in drawdown_thresholds:
            if d >= 100.0:
                steps = 100.0 / risk_per_trade_pct
            else:
                steps = math.log(1.0 - (d / 100.0)) / math.log(1.0 - f_frac)
            ror_pct = min(100.0, max(0.0, (z_root ** steps) * 100.0))
            probs[f"{int(d)}pct"] = round(ror_pct, 2)

        if risk_per_trade_pct > kelly_pct:
            sizing_status = "OVER_KELLY"
        elif risk_per_trade_pct > half_kelly_pct:
            sizing_status = "AGGRESSIVE"
        else:
            sizing_status = "CONSERVATIVE"

    return {
        "effective_win_rate_pct": round(p * 100.0, 2),
        "payoff_ratio": round(b, 3),
        "edge_per_decisive_r": round(edge, 4),
        "kelly_pct": round(kelly_pct, 2),
        "half_kelly_pct": round(half_kelly_pct, 2),
        "current_risk_pct": risk_per_trade_pct,
        "sizing_status": sizing_status,
        "drawdown_probabilities": probs,
    }


def run_monte_carlo_and_projections(
    r_multiples: Sequence[float],
    current_equity: float,
    risk_per_trade_pct: float = 0.5,
    avg_trades_per_day: float = 1.5,
    num_simulations: int = 5000,
    horizons_days: Sequence[int] = (30, 63, 126, 252),
    seed: int = 42,
) -> Dict[str, Any]:
    """Runs bootstrapped Monte Carlo simulations for empirical Risk of Ruin and future projections."""
    n_obs = len(r_multiples)
    if n_obs < 10:
        confidence = "INSUFFICIENT"
    elif n_obs < 30:
        confidence = "LOW"
    else:
        confidence = "MEANINGFUL"

    if n_obs == 0 or current_equity <= 0:
        return {
            "sample_size": n_obs,
            "confidence": confidence,
            "num_simulations": 0,
            "avg_trades_per_day": round(avg_trades_per_day, 2),
            "empirical_ror": {
                "10pct": None,
                "20pct": None,
                "50pct": None,
                "median_max_dd_pct": None,
                "p95_max_dd_pct": None,
                "expected_max_losing_streak": None,
            },
            "horizons": [],
        }

    rng = random.Random(seed)
    f_frac = risk_per_trade_pct / 100.0
    effective_tpd = max(avg_trades_per_day, 0.25)

    horizon_trade_counts = {
        d: max(1, int(round(d * effective_tpd))) for d in horizons_days
    }
    max_trades = max(252, max(horizon_trade_counts.values()))

    hit_10 = 0
    hit_20 = 0
    hit_50 = 0
    max_dds: List[float] = []
    max_streaks: List[int] = []
    horizon_equities: Dict[int, List[float]] = {d: [] for d in horizons_days}

    obs_list = list(r_multiples)

    for _ in range(num_simulations):
        eq = current_equity
        peak = current_equity
        max_dd = 0.0
        cur_streak = 0
        worst_streak = 0

        for step in range(1, max_trades + 1):
            r_val = rng.choice(obs_list)
            eq = max(0.0, eq * (1.0 + f_frac * r_val))
            if eq > peak:
                peak = eq
            dd = ((peak - eq) / peak) * 100.0 if peak > 0 else 100.0
            if dd > max_dd:
                max_dd = dd

            if r_val < -0.1:
                cur_streak += 1
                if cur_streak > worst_streak:
                    worst_streak = cur_streak
            else:
                cur_streak = 0

            for d, target_step in horizon_trade_counts.items():
                if step == target_step:
                    horizon_equities[d].append(eq)

        max_dds.append(max_dd)
        max_streaks.append(worst_streak)
        if max_dd >= 10.0:
            hit_10 += 1
        if max_dd >= 20.0:
            hit_20 += 1
        if max_dd >= 50.0:
            hit_50 += 1

    max_dds.sort()
    max_streaks.sort()

    # Deterministic geometric growth rate per trade
    mean_log_growth = sum(
        math.log(max(1e-6, 1.0 + f_frac * r)) for r in obs_list
    ) / n_obs

    horizon_results = []
    for d in horizons_days:
        eqs = sorted(horizon_equities[d])
        n_trades_h = horizon_trade_counts[d]
        geom_eq = current_equity * math.exp(mean_log_growth * n_trades_h)

        p10 = _percentile(eqs, 10)
        p25 = _percentile(eqs, 25)
        p50 = _percentile(eqs, 50)
        p75 = _percentile(eqs, 75)
        p90 = _percentile(eqs, 90)

        horizon_results.append(
            {
                "trading_days": d,
                "label": {30: "30D (~1.5M)", 63: "1 Quarter (63D)", 126: "6 Months (126D)", 252: "1 Year (252D)"}.get(d, f"{d}D"),
                "projected_trades": n_trades_h,
                "p10_equity": round(p10, 2),
                "p25_equity": round(p25, 2),
                "p50_equity": round(p50, 2),
                "p75_equity": round(p75, 2),
                "p90_equity": round(p90, 2),
                "geometric_equity": round(geom_eq, 2),
                "p10_roi_pct": round(((p10 - current_equity) / current_equity) * 100.0, 2),
                "p50_roi_pct": round(((p50 - current_equity) / current_equity) * 100.0, 2),
                "p90_roi_pct": round(((p90 - current_equity) / current_equity) * 100.0, 2),
            }
        )

    return {
        "sample_size": n_obs,
        "confidence": confidence,
        "num_simulations": num_simulations,
        "avg_trades_per_day": round(effective_tpd, 2),
        "empirical_ror": {
            "10pct": round((hit_10 / num_simulations) * 100.0, 2),
            "20pct": round((hit_20 / num_simulations) * 100.0, 2),
            "50pct": round((hit_50 / num_simulations) * 100.0, 2),
            "median_max_dd_pct": round(_percentile(max_dds, 50), 2),
            "p95_max_dd_pct": round(_percentile(max_dds, 95), 2),
            "expected_max_losing_streak": int(round(_percentile(max_streaks, 50))),
            "p95_max_losing_streak": int(round(_percentile(max_streaks, 95))),
        },
        "horizons": horizon_results,
    }


def simulate_signal_counterfactual(
    close_price: float,
    or_low: float,
    atr: float,
    bars_after_signal: Sequence[Any],
    breakeven_trigger_r: float = 1.0,
    breakeven_offset: float = 0.02,
    take_profit_r: float = 2.0,
) -> Optional[Dict[str, Any]]:
    """Simulates a rejected breakout signal against subsequent 5m bars using exact pTrading bracket rules."""
    if not bars_after_signal or close_price <= 0:
        return None

    entry_price = round(close_price * 1.0005, 2)
    atr_stop = round(close_price - 1.5 * atr, 2)
    initial_stop = round(max(or_low, atr_stop), 2)
    risk_1r = round(entry_price - initial_stop, 4)
    if risk_1r <= 0:
        return None

    take_profit = round(entry_price + take_profit_r * risk_1r, 2)
    be_trigger = entry_price + breakeven_trigger_r * risk_1r
    be_stop = round(entry_price + breakeven_offset, 2)

    active_stop = initial_stop
    be_stepped = False
    max_high = entry_price
    min_low = entry_price

    outcome = "EOD_CLOSE"
    exit_price = float(bars_after_signal[-1].close)

    for b in bars_after_signal:
        bh = float(b.high)
        bl = float(b.low)
        if bh > max_high:
            max_high = bh
        if bl < min_low:
            min_low = bl

        # Conservative intrabar ordering: check active stop before take-profit
        if bl <= active_stop:
            exit_price = active_stop
            outcome = "BREAKEVEN" if be_stepped else "STOP_LOSS"
            break

        if bh >= take_profit:
            exit_price = take_profit
            outcome = "TAKE_PROFIT"
            break

        if not be_stepped and bh >= be_trigger:
            be_stepped = True
            active_stop = be_stop

    pnl_r = round((exit_price - entry_price) / risk_1r, 3)
    mfe_r = round((max_high - entry_price) / risk_1r, 3)
    mae_r = round((entry_price - min_low) / risk_1r, 3)

    return {
        "sim_outcome": outcome,
        "sim_exit_price": round(exit_price, 2),
        "sim_pnl_r": pnl_r,
        "sim_mfe_r": mfe_r,
        "sim_mae_r": mae_r,
    }


def calculate_strategy_health_score(
    cum_metrics: Dict[str, Any],
    roi_equity: Dict[str, Any],
    analytical_ror: Dict[str, Any],
    mc_ror: Dict[str, Any],
) -> Dict[str, Any]:
    """Calculates a transparent 0-100 Strategy Health Score across 5 weighted pillars."""
    n_trades = int(cum_metrics.get("total_trades", 0))
    if n_trades == 0:
        return {
            "score": 0,
            "grade": "N/A",
            "status": "NO_DATA",
            "pillars": {
                "edge_expectancy": {"score": 0, "max": 30},
                "risk_of_ruin": {"score": 0, "max": 25},
                "drawdown_stability": {"score": 0, "max": 20},
                "execution_quality": {"score": 0, "max": 15},
                "sample_confidence": {"score": 0, "max": 10},
            },
        }

    # 1. Edge & Expectancy (30 pts)
    exp_r = float(cum_metrics.get("expectancy_r", 0.0))
    pf = float(cum_metrics.get("profit_factor", 0.0))
    exp_pts = max(0.0, min(18.0, (exp_r / 0.40) * 18.0)) if exp_r > 0 else 0.0
    pf_pts = max(0.0, min(12.0, ((pf - 1.0) / 1.5) * 12.0)) if pf > 1.0 else 0.0
    edge_score = round(exp_pts + pf_pts, 1)

    # 2. Risk of Ruin & Sizing Safety (25 pts)
    mc_20 = mc_ror.get("20pct")
    a_20 = analytical_ror.get("drawdown_probabilities", {}).get("20pct")
    ror_val = float(mc_20) if mc_20 is not None else (float(a_20) if a_20 is not None else 50.0)
    if ror_val <= 1.0:
        ror_pts = 18.0
    elif ror_val <= 5.0:
        ror_pts = 14.0
    elif ror_val <= 15.0:
        ror_pts = 9.0
    elif ror_val <= 35.0:
        ror_pts = 4.0
    else:
        ror_pts = 0.0

    sizing_status = analytical_ror.get("sizing_status", "INSUFFICIENT_DATA")
    sizing_pts = 7.0 if sizing_status == "CONSERVATIVE" else (4.0 if sizing_status == "AGGRESSIVE" else 0.0)
    ror_score = round(ror_pts + sizing_pts, 1)

    # 3. Drawdown & Equity Stability (20 pts)
    max_dd = float(roi_equity.get("max_drawdown_pct", 0.0))
    sqn = float(cum_metrics.get("sqn", 0.0))
    dd_pts = max(0.0, min(12.0, 12.0 * (1.0 - (max_dd / 20.0))))
    sqn_pts = max(0.0, min(8.0, (sqn / 2.0) * 8.0)) if sqn > 0 else 0.0
    if roi_equity.get("circuit_breaker_tripped_today"):
        dd_pts = max(0.0, dd_pts - 4.0)
    stability_score = round(dd_pts + sqn_pts, 1)

    # 4. Execution & Trade Quality (15 pts)
    realized_rr = float(cum_metrics.get("realized_rr_r", 0.0))
    rr_pts = max(0.0, min(7.0, (realized_rr / 2.0) * 7.0))
    avg_slip_r = abs(float(cum_metrics.get("slippage", {}).get("avg_slippage_r", 0.0)))
    # Positive slippage is adverse; negative slippage means price improvement
    raw_slip_r = float(cum_metrics.get("slippage", {}).get("avg_slippage_r", 0.0))
    slip_pts = 4.0 if raw_slip_r <= 0.05 else (2.0 if raw_slip_r <= 0.10 else 0.0)
    edge_ratio = cum_metrics.get("mfe_mae", {}).get("edge_ratio")
    mfe_pts = max(0.0, min(4.0, (float(edge_ratio) / 1.5) * 4.0)) if edge_ratio is not None else 2.0
    exec_score = round(rr_pts + slip_pts + mfe_pts, 1)

    # 5. Statistical Confidence / Sample Size (10 pts)
    sample_score = round(min(10.0, (n_trades / 30.0) * 10.0), 1)

    total_score = int(round(edge_score + ror_score + stability_score + exec_score + sample_score))
    total_score = max(0, min(100, total_score))

    if total_score >= 85:
        grade = "A"
    elif total_score >= 75:
        grade = "B+"
    elif total_score >= 65:
        grade = "B"
    elif total_score >= 55:
        grade = "C"
    elif total_score >= 40:
        grade = "D"
    else:
        grade = "F"

    if n_trades < 15:
        status = "PROMISING (EARLY SAMPLE)" if total_score >= 65 else "EARLY SAMPLE"
    elif total_score >= 75:
        status = "STRONG EDGE"
    elif total_score >= 55:
        status = "HEALTHY / MONITOR"
    else:
        status = "AT RISK"

    return {
        "score": total_score,
        "grade": grade,
        "status": status,
        "pillars": {
            "edge_expectancy": {"score": edge_score, "max": 30},
            "risk_of_ruin": {"score": ror_score, "max": 25},
            "drawdown_stability": {"score": stability_score, "max": 20},
            "execution_quality": {"score": exec_score, "max": 15},
            "sample_confidence": {"score": sample_score, "max": 10},
        },
    }


class AnalyticsService:
    """Computes daily & cumulative P&L, ROI, R:R, MFE/MAE, execution diagnostics, RoR, and projections."""

    def __init__(self) -> None:
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)

    @staticmethod
    def _trade_r(trade: Dict[str, Any]) -> Optional[float]:
        if trade.get("pnl_r") is not None:
            return float(trade["pnl_r"])
        pnl = trade.get("pnl_dollars")
        risk = trade.get("initial_risk_dollars")
        if pnl is not None and risk and float(risk) > 0:
            return round(float(pnl) / float(risk), 4)
        return None

    def compute_trade_metrics(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Computes P&L, R:R, Expectancy, SQN, MFE/MAE, slippage, and bucket attribution for a list of closed trades."""
        total = len(trades)
        if total == 0:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "breakevens": 0,
                "win_rate_pct": 0.0,
                "loss_rate_pct": 0.0,
                "breakeven_rate_pct": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "net_pnl": 0.0,
                "avg_win_dollars": 0.0,
                "avg_loss_dollars": 0.0,
                "largest_win_dollars": 0.0,
                "largest_loss_dollars": 0.0,
                "total_r": 0.0,
                "avg_win_r": 0.0,
                "avg_loss_r": 0.0,
                "planned_rr": 2.0,
                "realized_rr_dollars": 0.0,
                "realized_rr_r": 0.0,
                "expectancy_r": 0.0,
                "expectancy_dollars": 0.0,
                "profit_factor": 0.0,
                "sqn": 0.0,
                "total_risked_dollars": 0.0,
                "return_on_risked_capital_pct": 0.0,
                "max_consecutive_wins": 0,
                "max_consecutive_losses": 0,
                "exit_reason_breakdown": {},
                "symbol_breakdown": [],
                "mfe_mae": {
                    "sample_count": 0,
                    "avg_mfe_r": None,
                    "avg_mae_r": None,
                    "edge_ratio": None,
                    "avg_winner_mfe_r": None,
                    "avg_loser_mae_r": None,
                },
                "breakeven_audit": {
                    "stepped_count": 0,
                    "stepped_pct": 0.0,
                    "reached_tp_after_be": 0,
                    "stopped_at_be": 0,
                    "choked_winners_count": 0,
                    "choked_winners_pct": 0.0,
                },
                "slippage": {
                    "sample_count": 0,
                    "avg_slippage_dollars": 0.0,
                    "avg_slippage_r": 0.0,
                    "total_slippage_dollars": 0.0,
                },
                "time_of_day_buckets": {},
                "stop_type_buckets": {},
                "rvol_buckets": {},
                "atr_ext_buckets": {},
                "chartmill_attribution": {
                    "setup_rating_buckets": {},
                    "tech_rating_buckets": {},
                    "fundamental_rating_buckets": {},
                    "sector_buckets": {},
                },
                "cluster_stop_events": [],
                "r_distribution": {},
                "rolling_expectancy_10": None,
                "rolling_expectancy_20": None,
            }

        win_dollars: List[float] = []
        loss_dollars: List[float] = []
        win_rs: List[float] = []
        loss_rs: List[float] = []
        all_rs: List[float] = []
        all_pnls: List[float] = []
        total_risked = 0.0

        wins = 0
        losses = 0
        breakevens = 0
        cur_w = cur_l = max_w = max_l = 0

        exit_reasons: Dict[str, Dict[str, Any]] = {}
        by_symbol: Dict[str, Dict[str, Any]] = {}
        tod_map: Dict[str, List[float]] = {}
        stop_map: Dict[str, List[float]] = {}
        rvol_map: Dict[str, List[float]] = {}
        atr_map: Dict[str, List[float]] = {}
        cm_setup_map: Dict[str, List[float]] = {}
        cm_tech_map: Dict[str, List[float]] = {}
        cm_fund_map: Dict[str, List[float]] = {}
        cm_sector_map: Dict[str, List[float]] = {}

        mfe_vals: List[float] = []
        mae_vals: List[float] = []
        winner_mfes: List[float] = []
        loser_maes: List[float] = []

        be_stepped_count = 0
        be_then_tp = 0
        be_stopped = 0
        be_choked = 0

        slip_dollars_list: List[float] = []
        slip_r_list: List[float] = []
        stop_out_times: List[Tuple[datetime, str]] = []

        r_dist = {"<= -1.0R": 0, "-1.0R to -0.1R": 0, "Breakeven": 0, "+0.1R to +1.0R": 0, "+1.0R to +1.9R": 0, ">= +2.0R": 0}

        for t in trades:
            pnl = float(t.get("pnl_dollars") or 0.0)
            r_val = self._trade_r(t)
            r_eff = r_val if r_val is not None else 0.0
            all_pnls.append(pnl)
            all_rs.append(r_eff)

            risk_d = float(t.get("initial_risk_dollars") or 0.0)
            if risk_d > 0:
                total_risked += risk_d

            # Outcome classification
            if r_val is not None:
                if r_val > 0.1:
                    outcome = "WIN"
                elif r_val < -0.1:
                    outcome = "LOSS"
                else:
                    outcome = "BE"
            else:
                outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE")

            if outcome == "WIN":
                wins += 1
                win_dollars.append(pnl)
                win_rs.append(r_eff)
                cur_w += 1
                cur_l = 0
                max_w = max(max_w, cur_w)
            elif outcome == "LOSS":
                losses += 1
                loss_dollars.append(abs(pnl))
                loss_rs.append(abs(r_eff))
                cur_l += 1
                cur_w = 0
                max_l = max(max_l, cur_l)
            else:
                breakevens += 1
                cur_w = 0
                cur_l = 0

            # R-multiple histogram bucket
            if r_eff <= -1.0:
                r_dist["<= -1.0R"] += 1
            elif r_eff < -0.1:
                r_dist["-1.0R to -0.1R"] += 1
            elif r_eff <= 0.1:
                r_dist["Breakeven"] += 1
            elif r_eff < 1.0:
                r_dist["+0.1R to +1.0R"] += 1
            elif r_eff < 1.9:
                r_dist["+1.0R to +1.9R"] += 1
            else:
                r_dist[">= +2.0R"] += 1

            # Exit reason breakdown
            reason = str(t.get("exit_reason") or "UNKNOWN")
            er = exit_reasons.setdefault(reason, {"count": 0, "net_pnl": 0.0, "total_r": 0.0})
            er["count"] += 1
            er["net_pnl"] = round(er["net_pnl"] + pnl, 2)
            er["total_r"] = round(er["total_r"] + r_eff, 2)

            # Symbol breakdown
            sym = str(t.get("symbol") or "UNKNOWN")
            sb = by_symbol.setdefault(sym, {"symbol": sym, "trades": 0, "wins": 0, "net_pnl": 0.0, "total_r": 0.0})
            sb["trades"] += 1
            if outcome == "WIN":
                sb["wins"] += 1
            sb["net_pnl"] = round(sb["net_pnl"] + pnl, 2)
            sb["total_r"] = round(sb["total_r"] + r_eff, 2)

            # MFE / MAE
            if t.get("mfe_r") is not None:
                mfe = float(t["mfe_r"])
                mfe_vals.append(mfe)
                if outcome == "WIN":
                    winner_mfes.append(mfe)
            if t.get("mae_r") is not None:
                mae = abs(float(t["mae_r"]))
                mae_vals.append(mae)
                if outcome == "LOSS":
                    loser_maes.append(mae)

            # Breakeven audit
            if t.get("breakeven_stepped"):
                be_stepped_count += 1
                if reason == "TAKE_PROFIT":
                    be_then_tp += 1
                elif reason == "BREAKEVEN" or outcome == "BE":
                    be_stopped += 1
                    if t.get("post_exit_max_r") is not None and float(t["post_exit_max_r"]) >= 2.0:
                        be_choked += 1

            # Slippage
            if t.get("slippage_dollars") is not None:
                slip_dollars_list.append(float(t["slippage_dollars"]) * int(t.get("qty") or 1))
            if t.get("slippage_r") is not None:
                slip_r_list.append(float(t["slippage_r"]))

            # Time-of-day bucket
            tod = _time_bucket_et(t.get("entry_time"), self.tz)
            tod_map.setdefault(tod, []).append(r_eff)

            # Stop type bucket
            if t.get("stop_type"):
                stop_map.setdefault(str(t["stop_type"]), []).append(r_eff)

            # RVOL bucket
            if t.get("signal_rvol") is not None:
                rv = float(t["signal_rvol"])
                rk = "1.5-2.0" if rv < 2.0 else ("2.0-3.0" if rv < 3.0 else "3.0+")
                rvol_map.setdefault(rk, []).append(r_eff)

            # ATR extension bucket
            if t.get("signal_atr_ext") is not None:
                ae = float(t["signal_atr_ext"])
                ak = "0.0-0.25" if ae < 0.25 else ("0.25-0.50" if ae < 0.50 else "0.50-0.75+")
                atr_map.setdefault(ak, []).append(r_eff)

            # ChartMill rating & sector attribution buckets
            if t.get("cm_setup_rating") is not None:
                sr = float(t["cm_setup_rating"])
                cm_setup_map.setdefault("Setup >= 8" if sr >= 8.0 else "Setup < 8", []).append(r_eff)
            if t.get("cm_tech_rating") is not None:
                tr_v = float(t["cm_tech_rating"])
                cm_tech_map.setdefault("Tech >= 8" if tr_v >= 8.0 else "Tech < 8", []).append(r_eff)
            if t.get("cm_fundamental_rating") is not None:
                fr = float(t["cm_fundamental_rating"])
                cm_fund_map.setdefault("Fund >= 5" if fr >= 5.0 else "Fund < 5", []).append(r_eff)
            if t.get("cm_sector"):
                cm_sector_map.setdefault(str(t["cm_sector"]), []).append(r_eff)

            # Cluster stop-out tracking
            if reason == "STOP_LOSS":
                ex_dt = _parse_iso_dt(t.get("exit_time"))
                if ex_dt:
                    stop_out_times.append((ex_dt, sym))

        def _summarize_buckets(bmap: Dict[str, List[float]]) -> Dict[str, Dict[str, Any]]:
            out = {}
            for k, vals in bmap.items():
                w = sum(1 for v in vals if v > 0.1)
                out[k] = {
                    "trades": len(vals),
                    "win_rate_pct": round((w / len(vals)) * 100.0, 1) if vals else 0.0,
                    "avg_r": round(sum(vals) / len(vals), 3) if vals else 0.0,
                    "total_r": round(sum(vals), 2),
                }
            return out

        # Detect cluster stop-outs (>= 2 stop-outs within 15 minutes)
        stop_out_times.sort(key=lambda x: x[0])
        cluster_events: List[Dict[str, Any]] = []
        i = 0
        while i < len(stop_out_times):
            j = i + 1
            syms = [stop_out_times[i][1]]
            while j < len(stop_out_times) and (stop_out_times[j][0] - stop_out_times[i][0]) <= timedelta(minutes=15):
                syms.append(stop_out_times[j][1])
                j += 1
            if len(syms) >= 2:
                cluster_events.append(
                    {
                        "timestamp": stop_out_times[i][0].isoformat(),
                        "count": len(syms),
                        "symbols": syms,
                    }
                )
                i = j
            else:
                i += 1

        gross_profit = round(sum(p for p in all_pnls if p > 0), 2)
        gross_loss = round(abs(sum(p for p in all_pnls if p < 0)), 2)
        net_pnl = round(sum(all_pnls), 2)

        avg_win_d = round(sum(win_dollars) / len(win_dollars), 2) if win_dollars else 0.0
        avg_loss_d = round(sum(loss_dollars) / len(loss_dollars), 2) if loss_dollars else 0.0
        avg_win_r = round(sum(win_rs) / len(win_rs), 3) if win_rs else 0.0
        avg_loss_r = round(sum(loss_rs) / len(loss_rs), 3) if loss_rs else 0.0

        win_rate = wins / total
        loss_rate = losses / total
        be_rate = breakevens / total

        realized_rr_d = round(avg_win_d / avg_loss_d, 3) if avg_loss_d > 0 else (avg_win_d if avg_win_d > 0 else 0.0)
        realized_rr_r = round(avg_win_r / avg_loss_r, 3) if avg_loss_r > 0 else (avg_win_r if avg_win_r > 0 else 0.0)

        expectancy_r = round((win_rate * avg_win_r) - (loss_rate * avg_loss_r), 4)
        expectancy_d = round(net_pnl / total, 2)
        profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else (99.99 if gross_profit > 0 else 0.0)

        if len(all_rs) >= 2:
            std_r = statistics.stdev(all_rs)
            sqn = round((math.sqrt(len(all_rs)) * (sum(all_rs) / len(all_rs))) / std_r, 2) if std_r > 0 else 0.0
        else:
            sqn = 0.0

        avg_mfe = round(sum(mfe_vals) / len(mfe_vals), 3) if mfe_vals else None
        avg_mae = round(sum(mae_vals) / len(mae_vals), 3) if mae_vals else None
        edge_ratio = round(avg_mfe / avg_mae, 2) if (avg_mfe is not None and avg_mae and avg_mae > 0) else None

        symbol_list = sorted(by_symbol.values(), key=lambda x: x["net_pnl"], reverse=True)

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "breakevens": breakevens,
            "win_rate_pct": round(win_rate * 100.0, 2),
            "loss_rate_pct": round(loss_rate * 100.0, 2),
            "breakeven_rate_pct": round(be_rate * 100.0, 2),
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "net_pnl": net_pnl,
            "avg_win_dollars": avg_win_d,
            "avg_loss_dollars": avg_loss_d,
            "largest_win_dollars": round(max(all_pnls), 2) if all_pnls else 0.0,
            "largest_loss_dollars": round(min(all_pnls), 2) if all_pnls else 0.0,
            "total_r": round(sum(all_rs), 2),
            "avg_win_r": avg_win_r,
            "avg_loss_r": avg_loss_r,
            "planned_rr": 2.0,
            "realized_rr_dollars": realized_rr_d,
            "realized_rr_r": realized_rr_r,
            "expectancy_r": expectancy_r,
            "expectancy_dollars": expectancy_d,
            "profit_factor": profit_factor,
            "sqn": sqn,
            "total_risked_dollars": round(total_risked, 2),
            "return_on_risked_capital_pct": round((net_pnl / total_risked) * 100.0, 2) if total_risked > 0 else 0.0,
            "max_consecutive_wins": max_w,
            "max_consecutive_losses": max_l,
            "exit_reason_breakdown": exit_reasons,
            "symbol_breakdown": symbol_list,
            "mfe_mae": {
                "sample_count": len(mfe_vals),
                "avg_mfe_r": avg_mfe,
                "avg_mae_r": avg_mae,
                "edge_ratio": edge_ratio,
                "avg_winner_mfe_r": round(sum(winner_mfes) / len(winner_mfes), 3) if winner_mfes else None,
                "avg_loser_mae_r": round(sum(loser_maes) / len(loser_maes), 3) if loser_maes else None,
            },
            "breakeven_audit": {
                "stepped_count": be_stepped_count,
                "stepped_pct": round((be_stepped_count / total) * 100.0, 1),
                "reached_tp_after_be": be_then_tp,
                "stopped_at_be": be_stopped,
                "choked_winners_count": be_choked,
                "choked_winners_pct": round((be_choked / be_stopped) * 100.0, 1) if be_stopped > 0 else 0.0,
            },
            "slippage": {
                "sample_count": len(slip_r_list),
                "avg_slippage_dollars": round(sum(slip_dollars_list) / len(slip_dollars_list), 2) if slip_dollars_list else 0.0,
                "avg_slippage_r": round(sum(slip_r_list) / len(slip_r_list), 4) if slip_r_list else 0.0,
                "total_slippage_dollars": round(sum(slip_dollars_list), 2) if slip_dollars_list else 0.0,
            },
            "time_of_day_buckets": _summarize_buckets(tod_map),
            "stop_type_buckets": _summarize_buckets(stop_map),
            "rvol_buckets": _summarize_buckets(rvol_map),
            "atr_ext_buckets": _summarize_buckets(atr_map),
            "chartmill_attribution": {
                "setup_rating_buckets": _summarize_buckets(cm_setup_map),
                "tech_rating_buckets": _summarize_buckets(cm_tech_map),
                "fundamental_rating_buckets": _summarize_buckets(cm_fund_map),
                "sector_buckets": _summarize_buckets(cm_sector_map),
            },
            "cluster_stop_events": cluster_events,
            "r_distribution": r_dist,
            "rolling_expectancy_10": round(sum(all_rs[-10:]) / 10.0, 4) if len(all_rs) >= 10 else None,
            "rolling_expectancy_20": round(sum(all_rs[-20:]) / 20.0, 4) if len(all_rs) >= 20 else None,
        }

    def compute_roi_and_equity(
        self, daily_records_asc: List[Dict[str, Any]], target_date: str
    ) -> Dict[str, Any]:
        """Computes Daily/Cumulative/Rolling ROI, CAGR, Drawdown, Sharpe/Sortino, and equity curve."""
        if not daily_records_asc:
            return {
                "trading_days": 0,
                "initial_equity": 0.0,
                "current_equity": 0.0,
                "daily_starting_equity": 0.0,
                "daily_ending_equity": 0.0,
                "daily_pnl": 0.0,
                "daily_roi_pct": 0.0,
                "cumulative_pnl": 0.0,
                "cumulative_roi_pct": 0.0,
                "wtd_roi_pct": 0.0,
                "mtd_roi_pct": 0.0,
                "rolling_30d_roi_pct": 0.0,
                "annualized_roi_cagr_pct": None,
                "cagr_low_sample": True,
                "max_drawdown_dollars": 0.0,
                "max_drawdown_pct": 0.0,
                "current_drawdown_pct": 0.0,
                "recovery_factor": 0.0,
                "sharpe_ratio": None,
                "sortino_ratio": None,
                "daily_spy_return_pct": None,
                "daily_qqq_return_pct": None,
                "daily_alpha_vs_spy_pct": None,
                "market_regime": "UNKNOWN",
                "circuit_breaker_tripped_today": False,
                "equity_curve": [],
            }

        # Filter up to target_date if historical view requested
        scoped = [d for d in daily_records_asc if d["date"] <= target_date]
        if not scoped:
            scoped = daily_records_asc

        initial_equity = float(scoped[0]["starting_equity"])
        latest = scoped[-1]
        current_equity = float(latest["current_equity"])

        target_row = next((d for d in scoped if d["date"] == target_date), latest)
        d_start = float(target_row["starting_equity"])
        d_end = float(target_row["current_equity"])
        daily_pnl = round(d_end - d_start, 2)
        daily_roi_pct = round(((d_end - d_start) / d_start) * 100.0, 3) if d_start > 0 else 0.0

        # Time-Weighted Return (TWR) chains daily returns so deposits/account resets never distort ROI
        def _twr_roi(rows: List[Dict[str, Any]]) -> Tuple[float, float]:
            if not rows:
                return 0.0, 0.0
            growth = 1.0
            total_pnl = 0.0
            for r in rows:
                s_e = float(r["starting_equity"])
                e_e = float(r["current_equity"])
                total_pnl += (e_e - s_e)
                if s_e > 0:
                    growth *= (1.0 + ((e_e - s_e) / s_e))
            return round((growth - 1.0) * 100.0, 3), round(total_pnl, 2)

        cumulative_roi_pct, cumulative_pnl = _twr_roi(scoped)
        cumulative_roi_dec = cumulative_roi_pct / 100.0

        # WTD, MTD, Rolling 30D (Time-Weighted)
        target_dt = datetime.strptime(target_row["date"], "%Y-%m-%d").date()
        week_start = (target_dt - timedelta(days=target_dt.weekday())).strftime("%Y-%m-%d")
        month_start = target_dt.replace(day=1).strftime("%Y-%m-%d")

        wtd_roi, _ = _twr_roi([r for r in scoped if r["date"] >= week_start])
        mtd_roi, _ = _twr_roi([r for r in scoped if r["date"] >= month_start])
        rolling_30d_roi, _ = _twr_roi(scoped[-30:])

        trading_days = len(scoped)
        if trading_days > 0 and cumulative_roi_dec > -1.0:
            cagr = (((1.0 + cumulative_roi_dec) ** (252.0 / trading_days)) - 1.0) * 100.0
            cagr_val: Optional[float] = round(max(-100.0, min(cagr, 10000.0)), 2)
        else:
            cagr_val = None

        # Equity curve (using Time-Weighted Indexed Equity anchored to current account scale so resets don't spike),
        # drawdowns, and daily returns
        twr_base = current_equity / (1.0 + cumulative_roi_dec) if (1.0 + cumulative_roi_dec) > 0 else current_equity
        twr_growth = 1.0
        peak = twr_base
        max_dd_d = 0.0
        max_dd_pct = 0.0
        daily_returns: List[float] = []
        curve: List[Dict[str, Any]] = []

        for r in scoped:
            s_e = float(r["starting_equity"])
            c_e = float(r["current_equity"])
            ret = (c_e - s_e) / s_e if s_e > 0 else 0.0
            daily_returns.append(ret)

            twr_growth *= (1.0 + ret)
            twr_eq = twr_base * twr_growth

            if twr_eq > peak:
                peak = twr_eq
            dd_d = max(0.0, peak - twr_eq)
            dd_pct = (dd_d / peak) * 100.0 if peak > 0 else 0.0
            if dd_d > max_dd_d:
                max_dd_d = dd_d
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

            curve.append(
                {
                    "date": r["date"],
                    "equity": round(c_e, 2),
                    "twr_equity": round(twr_eq, 2),
                    "daily_pnl": round(c_e - s_e, 2),
                    "daily_roi_pct": round(ret * 100.0, 3),
                    "drawdown_pct": round(-dd_pct, 3),
                }
            )

        current_dd_pct = round(((peak - (twr_base * twr_growth)) / peak) * 100.0, 2) if peak > 0 else 0.0
        recovery_factor = round(cumulative_pnl / max_dd_d, 2) if max_dd_d > 0 else 0.0

        sharpe: Optional[float] = None
        sortino: Optional[float] = None
        if len(daily_returns) >= 5:
            mean_ret = sum(daily_returns) / len(daily_returns)
            std_ret = statistics.stdev(daily_returns)
            if std_ret > 0:
                sharpe = round((mean_ret / std_ret) * math.sqrt(252.0), 2)
            neg_rets = [x for x in daily_returns if x < 0]
            if len(neg_rets) >= 2:
                down_std = statistics.stdev(neg_rets)
                if down_std > 0:
                    sortino = round((mean_ret / down_std) * math.sqrt(252.0), 2)

        spy_ret = target_row.get("spy_return_pct")
        qqq_ret = target_row.get("qqq_return_pct")
        alpha_spy = round(daily_roi_pct - float(spy_ret), 3) if spy_ret is not None else None

        return {
            "trading_days": trading_days,
            "initial_equity": round(initial_equity, 2),
            "current_equity": round(current_equity, 2),
            "daily_starting_equity": round(d_start, 2),
            "daily_ending_equity": round(d_end, 2),
            "daily_pnl": daily_pnl,
            "daily_roi_pct": daily_roi_pct,
            "cumulative_pnl": cumulative_pnl,
            "cumulative_roi_pct": cumulative_roi_pct,
            "wtd_roi_pct": wtd_roi,
            "mtd_roi_pct": mtd_roi,
            "rolling_30d_roi_pct": rolling_30d_roi,
            "annualized_roi_cagr_pct": cagr_val,
            "cagr_low_sample": trading_days < 20,
            "max_drawdown_dollars": round(max_dd_d, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "current_drawdown_pct": current_dd_pct,
            "recovery_factor": recovery_factor,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "daily_spy_return_pct": round(float(spy_ret), 3) if spy_ret is not None else None,
            "daily_qqq_return_pct": round(float(qqq_ret), 3) if qqq_ret is not None else None,
            "daily_alpha_vs_spy_pct": alpha_spy,
            "market_regime": target_row.get("market_regime") or "UNKNOWN",
            "circuit_breaker_tripped_today": bool(target_row.get("circuit_breaker_tripped", 0)),
            "equity_curve": curve,
        }

    async def enrich_eod_excursions_and_regime(self, report_date: str) -> None:
        """Uses Alpaca 5m historical bars to populate MFE/MAE, post-exit continuation, and SPY/QQQ regime."""
        try:
            from app.services.market_scanner import get_market_scanner
            scanner = get_market_scanner()
        except Exception as e:
            logger.debug("Skipping EOD bar enrichment (scanner unavailable): %s", e)
            return

        day_dt = datetime.strptime(report_date, "%Y-%m-%d").date()
        session_start = datetime.combine(day_dt, time(9, 30), tzinfo=self.tz)
        session_end = datetime.combine(day_dt, time(16, 0), tzinfo=self.tz)

        # 1. Fetch SPY/QQQ for market regime & benchmark returns
        try:
            idx_bars = await scanner.fetch_bars_multi(["SPY", "QQQ"], session_start, session_end)
            spy_bars = idx_bars.get("SPY", [])
            qqq_bars = idx_bars.get("QQQ", [])

            spy_ret = None
            qqq_ret = None
            regime = "UNKNOWN"

            if spy_bars:
                spy_open = float(spy_bars[0].open)
                spy_close = float(spy_bars[-1].close)
                spy_ret = round(((spy_close - spy_open) / spy_open) * 100.0, 3) if spy_open > 0 else 0.0
                # Check % of bars above cumulative VWAP
                above = 0
                for idx in range(len(spy_bars)):
                    vwap = scanner.calculate_intraday_vwap(spy_bars[: idx + 1])
                    if float(spy_bars[idx].close) >= vwap:
                        above += 1
                pct_above = above / len(spy_bars)
                if spy_ret >= 0.4 and pct_above >= 0.65:
                    regime = "TREND_UP"
                elif spy_ret <= -0.4 and pct_above <= 0.35:
                    regime = "RISK_OFF"
                else:
                    regime = "CHOPPY"

            if qqq_bars:
                qqq_open = float(qqq_bars[0].open)
                qqq_close = float(qqq_bars[-1].close)
                qqq_ret = round(((qqq_close - qqq_open) / qqq_open) * 100.0, 3) if qqq_open > 0 else 0.0

            async with get_connection() as conn:
                await conn.execute(
                    """
                    UPDATE daily_stats
                    SET spy_return_pct = COALESCE(?, spy_return_pct),
                        qqq_return_pct = COALESCE(?, qqq_return_pct),
                        market_regime = COALESCE(?, market_regime)
                    WHERE date = ?;
                    """,
                    (spy_ret, qqq_ret, regime, report_date),
                )
                await conn.commit()
        except Exception as e:
            logger.warning("EOD SPY/QQQ regime enrichment failed: %s", e)

        # 2. Fetch 5m bars for today's closed trades missing MFE/MAE
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM trades
                WHERE is_open = 0
                  AND COALESCE(trade_date, substr(entry_time, 1, 10)) = ?
                  AND (mfe_r IS NULL OR mae_r IS NULL);
                """,
                (report_date,),
            )
            trades = [dict(r) for r in await cursor.fetchall()]

        if not trades:
            return

        symbols = sorted({t["symbol"] for t in trades})
        try:
            bars_by_sym = await scanner.fetch_bars_multi(symbols, session_start, session_end)
        except Exception as e:
            logger.warning("EOD trade bar fetch failed: %s", e)
            return

        async with get_connection() as conn:
            for t in trades:
                bars = bars_by_sym.get(t["symbol"], [])
                if not bars:
                    continue
                entry_p = float(t["entry_price"])
                init_stop = float(t.get("initial_stop_price") or t["stop_loss_price"])
                risk_1r = entry_p - init_stop
                if risk_1r <= 0:
                    continue

                entry_dt = _parse_iso_dt(t.get("entry_time"))
                exit_dt = _parse_iso_dt(t.get("exit_time"))
                if entry_dt and entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=ZoneInfo("UTC"))
                if exit_dt and exit_dt.tzinfo is None:
                    exit_dt = exit_dt.replace(tzinfo=ZoneInfo("UTC"))

                during_bars = []
                after_bars = []
                for b in bars:
                    bt = b.timestamp
                    if bt.tzinfo is None:
                        bt = bt.replace(tzinfo=ZoneInfo("UTC"))
                    if entry_dt and bt < entry_dt - timedelta(minutes=5):
                        continue
                    if exit_dt and bt > exit_dt + timedelta(minutes=5):
                        after_bars.append(b)
                    else:
                        during_bars.append(b)

                if during_bars:
                    max_h = max(float(b.high) for b in during_bars)
                    min_l = min(float(b.low) for b in during_bars)
                    mfe_r = round((max_h - entry_p) / risk_1r, 3)
                    mae_r = round((entry_p - min_l) / risk_1r, 3)
                else:
                    mfe_r = None
                    mae_r = None

                post_max_r = None
                if after_bars:
                    post_h = max(float(b.high) for b in after_bars)
                    post_max_r = round((post_h - entry_p) / risk_1r, 3)

                await conn.execute(
                    """
                    UPDATE trades
                    SET mfe_r = ?, mae_r = ?, post_exit_max_r = ?
                    WHERE id = ?;
                    """,
                    (mfe_r, mae_r, post_max_r, t["id"]),
                )
            await conn.commit()

        # 3. Evaluate counterfactual outcomes for REJECTED signals on report_date
        await self.enrich_missed_signals_counterfactual(report_date)

    async def enrich_missed_signals_counterfactual(self, report_date: str) -> int:
        """Simulates rejected signals on report_date against 5m Alpaca bars up to 15:55 ET."""
        try:
            from app.services.market_scanner import get_market_scanner
            scanner = get_market_scanner()
        except Exception:
            return 0

        day_dt = datetime.strptime(report_date, "%Y-%m-%d").date()
        session_start = datetime.combine(day_dt, time(9, 30), tzinfo=self.tz)
        flatten_cutoff = datetime.combine(day_dt, time(15, 55), tzinfo=self.tz)

        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM signals
                WHERE status = 'REJECTED'
                  AND substr(received_at, 1, 10) = ?
                  AND sim_outcome IS NULL;
                """,
                (report_date,),
            )
            rejected_rows = [dict(r) for r in await cursor.fetchall()]

        if not rejected_rows:
            return 0

        symbols = sorted({str(r["symbol"]).upper() for r in rejected_rows})
        try:
            bars_by_sym = await scanner.fetch_bars_multi(symbols, session_start, flatten_cutoff)
        except Exception as e:
            logger.warning("Failed to fetch 5m bars for missed signals audit: %s", e)
            return 0

        evaluated = 0
        async with get_connection() as conn:
            for sig in rejected_rows:
                sym = str(sig["symbol"]).upper()
                bars = bars_by_sym.get(sym, [])
                if not bars:
                    continue

                # Determine signal trigger time in UTC
                sig_dt = None
                if sig.get("bar_time"):
                    try:
                        sig_dt = datetime.fromtimestamp(int(sig["bar_time"]), tz=ZoneInfo("UTC"))
                    except Exception:
                        sig_dt = None
                if sig_dt is None:
                    sig_dt = _parse_iso_dt(sig.get("received_at"))
                    if sig_dt and sig_dt.tzinfo is None:
                        sig_dt = sig_dt.replace(tzinfo=ZoneInfo("UTC"))

                after_bars = []
                for b in bars:
                    bt = b.timestamp
                    if bt.tzinfo is None:
                        bt = bt.replace(tzinfo=ZoneInfo("UTC"))
                    if sig_dt and bt <= sig_dt:
                        continue
                    if bt.astimezone(self.tz) > flatten_cutoff:
                        continue
                    after_bars.append(b)

                sim = simulate_signal_counterfactual(
                    close_price=float(sig["close_price"]),
                    or_low=float(sig["or_low"]),
                    atr=float(sig["atr"]),
                    bars_after_signal=after_bars,
                    breakeven_trigger_r=settings.BREAKEVEN_TRIGGER_R,
                    breakeven_offset=settings.BREAKEVEN_OFFSET_DOLLARS,
                    take_profit_r=2.0,
                )
                if sim:
                    await conn.execute(
                        """
                        UPDATE signals
                        SET sim_outcome = ?, sim_exit_price = ?, sim_pnl_r = ?,
                            sim_mfe_r = ?, sim_mae_r = ?, sim_evaluated_at = CURRENT_TIMESTAMP
                        WHERE id = ?;
                        """,
                        (
                            sim["sim_outcome"],
                            sim["sim_exit_price"],
                            sim["sim_pnl_r"],
                            sim["sim_mfe_r"],
                            sim["sim_mae_r"],
                            sig["id"],
                        ),
                    )
                    evaluated += 1
            await conn.commit()

        logger.info("Counterfactual audit evaluated %d missed/rejected signal(s) for %s.", evaluated, report_date)
        return evaluated

    async def build_comprehensive_report(
        self,
        report_date: Optional[str] = None,
        num_simulations: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Builds the full daily + cumulative comprehensive analytics report for report_date."""
        target_date = report_date or datetime.now(self.tz).strftime("%Y-%m-%d")
        sims = num_simulations or settings.MONTE_CARLO_SIMULATIONS

        async with get_connection() as conn:
            daily_rows = [
                dict(r)
                for r in await (
                    await conn.execute("SELECT * FROM daily_stats ORDER BY date ASC;")
                ).fetchall()
            ]
            all_closed_trades = [
                dict(r)
                for r in await (
                    await conn.execute(
                        """
                        SELECT * FROM trades
                        WHERE is_open = 0 AND exit_price IS NOT NULL AND pnl_dollars IS NOT NULL
                        ORDER BY COALESCE(exit_time, entry_time) ASC;
                        """
                    )
                ).fetchall()
            ]
            day_signals = [
                dict(r)
                for r in await (
                    await conn.execute(
                        """
                        SELECT id, received_at, bar_time, symbol, close_price, or_high, or_low, atr,
                               status, rejection_reason, sim_outcome, sim_exit_price, sim_pnl_r, sim_mfe_r, sim_mae_r
                        FROM signals
                        WHERE substr(received_at, 1, 10) = ?
                        ORDER BY id ASC;
                        """,
                        (target_date,),
                    )
                ).fetchall()
            ]
            day_orders = [
                dict(r)
                for r in await (
                    await conn.execute(
                        "SELECT status FROM orders WHERE substr(created_at, 1, 10) = ?;",
                        (target_date,),
                    )
                ).fetchall()
            ]
            cm_rows = [
                dict(r)
                for r in await (
                    await conn.execute("SELECT * FROM chartmill_universe ORDER BY date ASC;")
                ).fetchall()
            ]

        # Build lookup for ChartMill technical/fundamental ratings by (date, symbol) with latest-symbol fallback
        cm_by_date_sym: Dict[Tuple[str, str], Dict[str, Any]] = {}
        cm_latest_by_sym: Dict[str, Dict[str, Any]] = {}
        for cm in cm_rows:
            sym_u = str(cm["symbol"]).upper()
            cm_by_date_sym[(str(cm["date"]), sym_u)] = cm
            cm_latest_by_sym[sym_u] = cm

        # Separate today's trades and cumulative trades up to target_date
        def _t_date(tr: Dict[str, Any]) -> str:
            return str(tr.get("trade_date") or (tr.get("entry_time") or "")[:10])

        for t in all_closed_trades:
            td = _t_date(t)
            sym_u = str(t.get("symbol") or "").upper()
            cm = cm_by_date_sym.get((td, sym_u)) or cm_latest_by_sym.get(sym_u)
            if cm:
                t["cm_setup_rating"] = cm.get("setup_rating")
                t["cm_tech_rating"] = cm.get("tech_rating")
                t["cm_rs"] = cm.get("relative_strength")
                t["cm_fundamental_rating"] = cm.get("fundamental_rating")
                t["cm_sector"] = cm.get("sector")

        cumulative_trades = [t for t in all_closed_trades if _t_date(t) <= target_date]
        daily_trades = [t for t in cumulative_trades if _t_date(t) == target_date]

        daily_metrics = self.compute_trade_metrics(daily_trades)
        cumulative_metrics = self.compute_trade_metrics(cumulative_trades)
        roi_equity = self.compute_roi_and_equity(daily_rows, target_date)

        # Signal funnel & Missed Signals Counterfactual Audit for target_date
        signal_funnel: Dict[str, int] = {}
        rejection_reasons: Dict[str, int] = {}
        missed_items: List[Dict[str, Any]] = []
        sim_wins = 0
        sim_losses = 0
        sim_bes = 0
        sim_r_total = 0.0
        concurrency_blocked = 0
        concurrency_r_total = 0.0
        one_r_dollars = (roi_equity["daily_starting_equity"] or 100000.0) * (settings.RISK_PERCENT_PER_TRADE / 100.0)

        for s in day_signals:
            st = str(s.get("status") or "UNKNOWN")
            signal_funnel[st] = signal_funnel.get(st, 0) + 1
            if s.get("rejection_reason"):
                rr = str(s["rejection_reason"])
                rejection_reasons[rr] = rejection_reasons.get(rr, 0) + 1

            if st == "REJECTED":
                ts_et = str(s.get("received_at") or "")
                dt_parsed = _parse_iso_dt(ts_et)
                if dt_parsed:
                    if dt_parsed.tzinfo is None:
                        dt_parsed = dt_parsed.replace(tzinfo=ZoneInfo("UTC"))
                    ts_et = dt_parsed.astimezone(self.tz).strftime("%H:%M:%S ET")

                sim_r = s.get("sim_pnl_r")
                sim_out = s.get("sim_outcome")
                is_conc = "concurrent" in str(s.get("rejection_reason") or "").lower()
                if is_conc:
                    concurrency_blocked += 1

                if sim_r is not None:
                    r_f = float(sim_r)
                    sim_r_total += r_f
                    if is_conc:
                        concurrency_r_total += r_f
                    if r_f > 0.1:
                        sim_wins += 1
                    elif r_f < -0.1:
                        sim_losses += 1
                    else:
                        sim_bes += 1

                missed_items.append(
                    {
                        "time_et": ts_et,
                        "symbol": s["symbol"],
                        "entry_price": round(float(s["close_price"]) * 1.0005, 2),
                        "rejection_reason": s.get("rejection_reason") or "",
                        "is_concurrency_cap": is_conc,
                        "sim_outcome": sim_out,
                        "sim_exit_price": s.get("sim_exit_price"),
                        "sim_pnl_r": round(float(sim_r), 2) if sim_r is not None else None,
                        "sim_pnl_dollars": round(float(sim_r) * one_r_dollars, 2) if sim_r is not None else None,
                        "sim_mfe_r": round(float(s["sim_mfe_r"]), 2) if s.get("sim_mfe_r") is not None else None,
                        "sim_mae_r": round(float(s["sim_mae_r"]), 2) if s.get("sim_mae_r") is not None else None,
                    }
                )

        eval_cnt = sim_wins + sim_losses + sim_bes
        missed_signals_audit = {
            "total_rejected": len(missed_items),
            "evaluated_count": eval_cnt,
            "concurrency_blocked_count": concurrency_blocked,
            "concurrency_net_r": round(concurrency_r_total, 2),
            "sim_wins": sim_wins,
            "sim_losses": sim_losses,
            "sim_breakevens": sim_bes,
            "sim_win_rate_pct": round((sim_wins / eval_cnt) * 100.0, 1) if eval_cnt > 0 else 0.0,
            "net_missed_r": round(sim_r_total, 2),
            "estimated_missed_dollars": round(sim_r_total * one_r_dollars, 2),
            "items": missed_items,
        }

        cancelled_orders_today = sum(
            1 for o in day_orders if str(o.get("status") or "").upper() == "CANCELLED"
        )

        # Analytical Risk of Ruin (from cumulative stats)
        analytical_ror = calculate_analytical_ror(
            win_rate=cumulative_metrics["win_rate_pct"] / 100.0,
            loss_rate=cumulative_metrics["loss_rate_pct"] / 100.0,
            payoff_ratio=cumulative_metrics["realized_rr_r"],
            risk_per_trade_pct=settings.RISK_PERCENT_PER_TRADE,
        )

        # Monte Carlo Empirical RoR & Future Projections
        r_series = [
            r for r in (self._trade_r(t) for t in cumulative_trades) if r is not None
        ]
        trading_days = max(roi_equity["trading_days"], 1)
        avg_tpd = len(cumulative_trades) / trading_days if cumulative_trades else 1.0
        base_equity = roi_equity["current_equity"] if roi_equity["current_equity"] > 0 else 50000.0

        mc_projections = run_monte_carlo_and_projections(
            r_multiples=r_series,
            current_equity=base_equity,
            risk_per_trade_pct=settings.RISK_PERCENT_PER_TRADE,
            avg_trades_per_day=avg_tpd,
            num_simulations=sims,
        )

        health_score = calculate_strategy_health_score(
            cum_metrics=cumulative_metrics,
            roi_equity=roi_equity,
            analytical_ror=analytical_ror,
            mc_ror=mc_projections["empirical_ror"],
        )

        return {
            "report_date": target_date,
            "generated_at": datetime.now(self.tz).isoformat(),
            "health_score": health_score,
            "strategy_params": {
                "risk_percent_per_trade": settings.RISK_PERCENT_PER_TRADE,
                "max_concurrent_positions": settings.MAX_CONCURRENT_POSITIONS,
                "daily_max_loss_percent": settings.DAILY_MAX_LOSS_PERCENT,
                "breakeven_trigger_r": settings.BREAKEVEN_TRIGGER_R,
                "take_profit_r": 2.0,
                "scanner_min_rvol": settings.SCANNER_MIN_RVOL,
                "scanner_max_atr_extension": settings.SCANNER_MAX_ATR_EXTENSION,
                "stale_order_timeout_seconds": settings.STALE_ORDER_TIMEOUT_SECONDS,
            },
            "roi_equity": roi_equity,
            "daily_trades_metrics": daily_metrics,
            "cumulative_trades_metrics": cumulative_metrics,
            "signal_funnel": {
                "total_signals": len(day_signals),
                "by_status": signal_funnel,
                "rejection_reasons": rejection_reasons,
                "expired_stale_orders": cancelled_orders_today,
            },
            "missed_signals_audit": missed_signals_audit,
            "risk_of_ruin": {
                "analytical": analytical_ror,
                "monte_carlo": mc_projections["empirical_ror"],
                "sample_size": mc_projections["sample_size"],
                "confidence": mc_projections["confidence"],
            },
            "projections": {
                "base_equity": round(base_equity, 2),
                "avg_trades_per_day": mc_projections["avg_trades_per_day"],
                "sample_size": mc_projections["sample_size"],
                "confidence": mc_projections["confidence"],
                "horizons": mc_projections["horizons"],
            },
        }


analytics_service = AnalyticsService()

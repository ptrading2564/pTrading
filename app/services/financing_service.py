"""Pragmatic Capital Governance, Paper-to-Live Qualification & Wealth-Building Service."""

from __future__ import annotations

import csv
import io
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.config import settings
from app.db import (
    get_capital_ledger,
    get_connection,
    get_financing_config,
    get_system_state,
    get_validation_checks,
    set_system_state,
    update_financing_config,
)
from app.services.analytics_service import analytics_service

logger = logging.getLogger(__name__)


RISK_PROFILES: Dict[str, Dict[str, Any]] = {
    "P1_DEFENSIVE": {
        "label": "P1 Defensive",
        "risk_pct": 0.25,
        "reserve_pct": 30.0,
        "description": "Capital preservation focus: 0.25% risk/trade, 30% protected reserve.",
    },
    "P2_BALANCED": {
        "label": "P2 Balanced",
        "risk_pct": 0.50,
        "reserve_pct": 20.0,
        "description": "Central comparator: 0.50% risk/trade, 20% protected reserve.",
    },
    "P3_GROWTH": {
        "label": "P3 Growth-Controlled",
        "risk_pct": 1.00,
        "reserve_pct": 10.0,
        "description": "Higher growth envelope: 1.00% risk/trade, 10% protected reserve.",
    },
    "CUSTOM": {
        "label": "Custom",
        "risk_pct": None,
        "reserve_pct": None,
        "description": "Uses RISK_PERCENT_PER_TRADE from config and custom reserve %.",
    },
}

VALID_LIFECYCLE_STAGES = {
    "PAPER_QUALIFICATION",
    "PAPER_QUALIFIED",
    "LIVE_PILOT",
    "LIVE_ACTIVE",
    "SUSPENDED",
    "REJECTED",
}

HEALTH_MULTIPLIERS: Dict[str, float] = {
    "HEALTHY": 1.00,
    "CAUTION": 0.75,
    "REDUCED_RISK": 0.50,
    "SUSPENDED": 0.00,
    "CAPITAL_PROTECTION": 0.00,
}

MANUAL_SAFETY_CHECKS: List[Dict[str, str]] = [
    {
        "check_id": "KILL_SWITCH_TESTED",
        "title": "Kill-Switch & Dead Man's Brake Verified",
        "description": "Tested emergency kill-switch and Dead Man's Brake latch/reset flow.",
    },
    {
        "check_id": "BREAKEVEN_STEP_VERIFIED",
        "title": "+1.0R Breakeven Stop Stepping Verified",
        "description": "Observed at least one paper trade cleanly step stop-loss to breakeven at +1.0R.",
    },
    {
        "check_id": "LIVE_CUSTODY_SET",
        "title": "Seed, Goal, Reserve & Live Ceiling Configured",
        "description": "Set real-money seed capital, target goal, reserve %, and Maximum Live Accessible Capital.",
    },
    {
        "check_id": "BROKER_LIVE_READY",
        "title": "Live Broker Account & Funding Buffer Ready",
        "description": "Verified live Alpaca keys, initial live pilot buffer, and external protected vault.",
    },
]

VALID_LEDGER_FLOW_TYPES = {
    "INITIAL_SEED",
    "DEPOSIT",
    "WITHDRAWAL",
    "VAULT_SWEEP",
    "VAULT_FUNDING",
}

FLOW_DIRECTIONS: Dict[str, str] = {
    "INITIAL_SEED": "IN_TO_BROKER",
    "DEPOSIT": "EXTERNAL_IN",
    "WITHDRAWAL": "EXTERNAL_OUT",
    "VAULT_SWEEP": "OUT_TO_VAULT",
    "VAULT_FUNDING": "IN_TO_BROKER",
}


@dataclass
class EffectiveRiskControls:
    allowed: bool
    block_reason: Optional[str]
    effective_equity: float
    base_risk_pct: float
    health_state: str
    health_multiplier: float
    pilot_multiplier: float
    combined_risk_multiplier: float
    effective_risk_pct: float
    current_drawdown_pct: float
    lifecycle_stage: str
    enforcement_enabled: bool
    brake_latched: bool


class FinancingService:
    """Manages Health-State Drawdown Ladder, Paper-to-Live Qualification, Dead Man's Brake, and Wealth Tracking."""

    def __init__(self) -> None:
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)

    @staticmethod
    def compute_health_state(
        current_dd_pct: float,
        previous_state: str = "HEALTHY",
        latched: bool = False,
        brake_latched: bool = False,
    ) -> Tuple[str, bool, float]:
        """Evaluates drawdown health state with hysteresis and conservative latch rules.

        Returns (state, is_latched, risk_multiplier).
        - Stepping down is immediate at 5%, 10%, 15%, 20%.
        - Stepping back up from CAUTION/REDUCED_RISK requires a 1.5% recovery buffer.
        - SUSPENDED (>=15%) and CAPITAL_PROTECTION (>=20% or brake) stay latched until explicit reset.
        """
        dd = max(0.0, float(current_dd_pct))

        if brake_latched or dd >= 20.0:
            return "CAPITAL_PROTECTION", True, 0.0

        if dd >= 15.0:
            return "SUSPENDED", True, 0.0

        if latched and previous_state in ("SUSPENDED", "CAPITAL_PROTECTION"):
            return previous_state, True, 0.0

        if dd >= 10.0:
            return "REDUCED_RISK", False, 0.50

        if previous_state == "REDUCED_RISK" and dd >= 8.5:
            return "REDUCED_RISK", False, 0.50

        if dd >= 5.0:
            return "CAUTION", False, 0.75

        if previous_state == "CAUTION" and dd >= 3.5:
            return "CAUTION", False, 0.75

        return "HEALTHY", False, 1.00

    async def get_current_drawdown_pct(self, live_equity: Optional[float] = None) -> float:
        """Computes cumulative TWR peak-to-trough drawdown % from daily_stats (plus optional live equity)."""
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT date, starting_equity, current_equity FROM daily_stats ORDER BY date ASC;"
            )
            rows = [dict(r) for r in await cursor.fetchall()]

        if not rows:
            return 0.0

        today_str = datetime.now(self.tz).strftime("%Y-%m-%d")
        twr_growth = 1.0
        peak = 1.0

        for idx, r in enumerate(rows):
            s_e = float(r["starting_equity"])
            c_e = float(r["current_equity"])
            if live_equity is not None and idx == len(rows) - 1 and r["date"] == today_str:
                c_e = float(live_equity)
            ret = (c_e - s_e) / s_e if s_e > 0 else 0.0
            twr_growth *= (1.0 + ret)
            if twr_growth > peak:
                peak = twr_growth

        dd_pct = ((peak - twr_growth) / peak) * 100.0 if peak > 0 else 0.0
        return round(max(0.0, dd_pct), 2)

    async def is_brake_latched(self) -> Tuple[bool, str]:
        """Returns (is_latched, reason) for the Dead Man's Brake."""
        state = await get_system_state("dead_mans_brake", "OFF")
        reason = await get_system_state("dead_mans_brake_reason", "") or ""
        return (state == "LATCHED"), reason

    async def latch_dead_mans_brake(self, reason: str) -> None:
        """Latches the Dead Man's Brake to fail-closed for all new entries until reconciled & reset."""
        clean_reason = reason.strip() or "Operational integrity fault"
        logger.critical("DEAD MAN'S BRAKE LATCHED: %s", clean_reason)
        await set_system_state("dead_mans_brake", "LATCHED")
        await set_system_state("dead_mans_brake_reason", clean_reason)
        await update_financing_config(
            {
                "current_health_state": "CAPITAL_PROTECTION",
                "health_state_latched": 1,
                "health_state_updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def reset_dead_mans_brake(self) -> None:
        """Explicitly clears the Dead Man's Brake after operator reconciliation."""
        logger.warning("Dead Man's Brake manually reset by operator.")
        await set_system_state("dead_mans_brake", "OFF")
        await set_system_state("dead_mans_brake_reason", "")
        await self.reset_health_latch()

    async def reset_health_latch(self) -> Dict[str, Any]:
        """Clears a latched SUSPENDED/CAPITAL_PROTECTION state and recomputes state from current DD."""
        brake_latched, _ = await self.is_brake_latched()
        dd_pct = await self.get_current_drawdown_pct()
        if brake_latched:
            new_state, is_latched = "CAPITAL_PROTECTION", True
        elif dd_pct >= 20.0:
            new_state, is_latched = "CAPITAL_PROTECTION", True
        elif dd_pct >= 15.0:
            # Allow deliberate reinstatement into REDUCED_RISK (50%) when operator explicitly resets after review
            new_state, is_latched = "REDUCED_RISK", False
        elif dd_pct >= 10.0:
            new_state, is_latched = "REDUCED_RISK", False
        elif dd_pct >= 5.0:
            new_state, is_latched = "CAUTION", False
        else:
            new_state, is_latched = "HEALTHY", False

        return await update_financing_config(
            {
                "current_health_state": new_state,
                "health_state_latched": 1 if is_latched else 0,
                "health_state_updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    @staticmethod
    def resolve_base_risk_pct(config: Dict[str, Any]) -> float:
        """Resolves base risk % from risk_profile (falling back to settings.RISK_PERCENT_PER_TRADE)."""
        profile_key = str(config.get("risk_profile") or "P2_BALANCED")
        profile = RISK_PROFILES.get(profile_key)
        if profile and profile.get("risk_pct") is not None:
            return float(profile["risk_pct"])
        return float(settings.RISK_PERCENT_PER_TRADE)

    async def get_effective_risk_controls(self, broker_equity: float) -> EffectiveRiskControls:
        """Computes active risk controls for RiskManager based on Dead Man's Brake, Health Ladder, Stage & Ceiling."""
        config = await get_financing_config()
        brake_latched, brake_reason = await self.is_brake_latched()
        dd_pct = await self.get_current_drawdown_pct(live_equity=broker_equity)

        prev_state = str(config.get("current_health_state") or "HEALTHY")
        prev_latched = bool(config.get("health_state_latched", 0))
        new_state, new_latched, health_mult = self.compute_health_state(
            current_dd_pct=dd_pct,
            previous_state=prev_state,
            latched=prev_latched,
            brake_latched=brake_latched,
        )

        # Persist state transitions
        if new_state != prev_state or new_latched != prev_latched:
            await update_financing_config(
                {
                    "current_health_state": new_state,
                    "health_state_latched": 1 if new_latched else 0,
                    "health_state_updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        lifecycle_stage = str(config.get("lifecycle_stage") or "PAPER_QUALIFICATION")
        enforcement_enabled = bool(config.get("enforcement_enabled", 1))
        base_risk_pct = self.resolve_base_risk_pct(config)

        # Dead Man's Brake ALWAYS blocks new entries (fail-closed operational safety)
        if brake_latched:
            return EffectiveRiskControls(
                allowed=False,
                block_reason=f"Dead Man's Brake LATCHED: {brake_reason or 'Manual reconciliation required.'}",
                effective_equity=broker_equity,
                base_risk_pct=base_risk_pct,
                health_state="CAPITAL_PROTECTION",
                health_multiplier=0.0,
                pilot_multiplier=1.0,
                combined_risk_multiplier=0.0,
                effective_risk_pct=0.0,
                current_drawdown_pct=dd_pct,
                lifecycle_stage=lifecycle_stage,
                enforcement_enabled=enforcement_enabled,
                brake_latched=True,
            )

        # If active enforcement is disabled (observe-only mode), pass through standard config
        if not enforcement_enabled:
            return EffectiveRiskControls(
                allowed=True,
                block_reason=None,
                effective_equity=broker_equity,
                base_risk_pct=float(settings.RISK_PERCENT_PER_TRADE),
                health_state=new_state,
                health_multiplier=health_mult,
                pilot_multiplier=1.0,
                combined_risk_multiplier=1.0,
                effective_risk_pct=float(settings.RISK_PERCENT_PER_TRADE),
                current_drawdown_pct=dd_pct,
                lifecycle_stage=lifecycle_stage,
                enforcement_enabled=False,
                brake_latched=False,
            )

        # Stage-level blocks
        if lifecycle_stage in ("SUSPENDED", "REJECTED"):
            return EffectiveRiskControls(
                allowed=False,
                block_reason=f"Financing lifecycle stage is {lifecycle_stage}. New entries blocked.",
                effective_equity=broker_equity,
                base_risk_pct=base_risk_pct,
                health_state=new_state,
                health_multiplier=health_mult,
                pilot_multiplier=1.0,
                combined_risk_multiplier=0.0,
                effective_risk_pct=0.0,
                current_drawdown_pct=dd_pct,
                lifecycle_stage=lifecycle_stage,
                enforcement_enabled=True,
                brake_latched=False,
            )

        # Health-state ladder blocks
        if new_state in ("SUSPENDED", "CAPITAL_PROTECTION") or health_mult <= 0.0:
            return EffectiveRiskControls(
                allowed=False,
                block_reason=(
                    f"Drawdown Health State is {new_state} (DD: {dd_pct:.2f}%). "
                    "New entries paused until review & manual reset on /financing."
                ),
                effective_equity=broker_equity,
                base_risk_pct=base_risk_pct,
                health_state=new_state,
                health_multiplier=0.0,
                pilot_multiplier=1.0,
                combined_risk_multiplier=0.0,
                effective_risk_pct=0.0,
                current_drawdown_pct=dd_pct,
                lifecycle_stage=lifecycle_stage,
                enforcement_enabled=True,
                brake_latched=False,
            )

        # Maximum Live Accessible Capital ceiling
        max_cap = config.get("max_live_accessible_capital")
        effective_equity = broker_equity
        if max_cap is not None and float(max_cap) > 0:
            effective_equity = min(broker_equity, float(max_cap))

        # Live Pilot scaling (e.g. 0.5x during initial live pilot)
        pilot_mult = (
            float(config.get("pilot_risk_multiplier") or 0.5)
            if lifecycle_stage == "LIVE_PILOT"
            else 1.0
        )
        combined_mult = round(health_mult * pilot_mult, 4)
        effective_risk_pct = round(base_risk_pct * combined_mult, 4)

        return EffectiveRiskControls(
            allowed=True,
            block_reason=None,
            effective_equity=effective_equity,
            base_risk_pct=base_risk_pct,
            health_state=new_state,
            health_multiplier=health_mult,
            pilot_multiplier=pilot_mult,
            combined_risk_multiplier=combined_mult,
            effective_risk_pct=effective_risk_pct,
            current_drawdown_pct=dd_pct,
            lifecycle_stage=lifecycle_stage,
            enforcement_enabled=True,
            brake_latched=False,
        )

    def evaluate_qualification(
        self,
        config: Dict[str, Any],
        report: Dict[str, Any],
        manual_checks: Dict[str, Dict[str, Any]],
        open_trades_count: int = 0,
    ) -> Dict[str, Any]:
        """Evaluates 6 auto-computed + 4 manual checks and produces a Go/No-Go verdict."""
        cum = report.get("cumulative_trades_metrics", {})
        roi = report.get("roi_equity", {})
        ror = report.get("risk_of_ruin", {})

        total_trades = int(cum.get("total_trades", 0))
        trading_days = int(roi.get("trading_days", 0))
        expectancy_r = float(cum.get("expectancy_r", 0.0))
        profit_factor = float(cum.get("profit_factor", 0.0))
        max_dd_pct = float(roi.get("max_drawdown_pct", 0.0))
        avg_slip_r = float(cum.get("slippage", {}).get("avg_slippage_r", 0.0))

        analytical = ror.get("analytical", {})
        mc_ror = ror.get("monte_carlo", {})
        ror_20 = mc_ror.get("20pct")
        if ror_20 is None:
            ror_20 = analytical.get("drawdown_probabilities", {}).get("20pct")
        sizing_status = analytical.get("sizing_status", "INSUFFICIENT_DATA")

        min_trades = int(config.get("qual_min_trades", 30))
        min_days = int(config.get("qual_min_trading_days", 10))
        min_exp = float(config.get("qual_min_expectancy_r", 0.15))
        min_pf = float(config.get("qual_min_profit_factor", 1.30))
        max_dd_limit = float(config.get("qual_max_drawdown_pct", 10.0))
        max_slip_limit = float(config.get("qual_max_avg_slippage_r", 0.10))
        max_ror_limit = float(config.get("qual_max_ror_20pct", 10.0))

        has_sample = total_trades >= min_trades and trading_days >= min_days

        # 6 Auto-evaluated checks
        auto_checks = [
            {
                "check_id": "SAMPLE_SIZE",
                "type": "AUTO",
                "title": "Minimum Sample Size",
                "target": f">= {min_trades} trades & >= {min_days} days",
                "actual": f"{total_trades} trades, {trading_days} days",
                "status": "PASSED" if has_sample else "PENDING",
            },
            {
                "check_id": "EDGE_EXPECTANCY",
                "type": "AUTO",
                "title": "Positive Edge & Profit Factor",
                "target": f"Exp >= +{min_exp:.2f}R & PF >= {min_pf:.2f}",
                "actual": f"{expectancy_r:+.3f}R | PF {profit_factor:.2f}",
                "status": (
                    "PENDING"
                    if total_trades == 0
                    else ("PASSED" if (expectancy_r >= min_exp and profit_factor >= min_pf) else "FAILED")
                ),
            },
            {
                "check_id": "DRAWDOWN_CONTROL",
                "type": "AUTO",
                "title": "Max Drawdown Within Limit",
                "target": f"< {max_dd_limit:.1f}%",
                "actual": f"{max_dd_pct:.2f}%",
                "status": (
                    "PENDING"
                    if trading_days == 0
                    else ("PASSED" if max_dd_pct < max_dd_limit else "FAILED")
                ),
            },
            {
                "check_id": "RISK_OF_RUIN",
                "type": "AUTO",
                "title": "Risk of Ruin & Kelly Sizing",
                "target": f"RoR(-20%) <= {max_ror_limit:.1f}% & <= Half-Kelly",
                "actual": (
                    f"{ror_20:.2f}% ({sizing_status})"
                    if ror_20 is not None
                    else f"Pending ({sizing_status})"
                ),
                "status": (
                    "PENDING"
                    if ror_20 is None or total_trades < 5
                    else (
                        "PASSED"
                        if (float(ror_20) <= max_ror_limit and sizing_status == "CONSERVATIVE")
                        else "FAILED"
                    )
                ),
            },
            {
                "check_id": "EXECUTION_SLIPPAGE",
                "type": "AUTO",
                "title": "Execution Slippage Control",
                "target": f"Avg <= {max_slip_limit:.2f}R",
                "actual": f"{avg_slip_r:+.3f}R",
                "status": (
                    "PENDING"
                    if total_trades == 0
                    else ("PASSED" if avg_slip_r <= max_slip_limit else "FAILED")
                ),
            },
            {
                "check_id": "EOD_FLATTEN_CLEAN",
                "type": "AUTO",
                "title": "Zero Overnight Exposure Integrity",
                "target": "0 unresolved open positions",
                "actual": f"{open_trades_count} open trade(s)",
                "status": "PASSED" if open_trades_count == 0 else "WARNING",
            },
        ]

        # 4 Manual/Interactive safety checks
        manual_list = []
        for meta in MANUAL_SAFETY_CHECKS:
            stored = manual_checks.get(meta["check_id"], {})
            manual_list.append(
                {
                    "check_id": meta["check_id"],
                    "type": "MANUAL",
                    "title": meta["title"],
                    "description": meta["description"],
                    "status": stored.get("status", "NOT_RUN"),
                    "notes": stored.get("notes", ""),
                    "verified_at": stored.get("verified_at"),
                }
            )

        auto_passed = sum(1 for c in auto_checks if c["status"] == "PASSED")
        manual_passed = sum(1 for c in manual_list if c["status"] == "PASSED")
        total_passed = auto_passed + manual_passed
        total_checks = len(auto_checks) + len(manual_list)

        # Go / No-Go Verdict logic
        fatal_dd = max_dd_pct >= 15.0
        early_negative_edge = total_trades >= 15 and expectancy_r < -0.10 and profit_factor < 0.85

        if fatal_dd or (has_sample and (expectancy_r <= 0.0 or profit_factor < 1.0)) or early_negative_edge:
            verdict = "NOT_VIABLE"
            verdict_label = "Not Viable — Do Not Go Live"
            verdict_summary = (
                f"Strategy failed viability thresholds (Max DD {max_dd_pct:.2f}%, "
                f"Expectancy {expectancy_r:+.3f}R, PF {profit_factor:.2f} across {total_trades} trades). "
                "Stop qualification and redesign/re-test before risking live capital."
            )
        elif not has_sample:
            verdict = "COLLECTING_EVIDENCE"
            verdict_label = "Collecting Paper Evidence"
            rem_trades = max(0, min_trades - total_trades)
            rem_days = max(0, min_days - trading_days)
            verdict_summary = (
                f"Paper qualification in progress: need {rem_trades} more closed trade(s) and "
                f"{rem_days} more trading day(s) to reach the Go/No-Go gate."
            )
        else:
            # Sample threshold reached and edge is positive
            failing_auto = [c["title"] for c in auto_checks if c["status"] != "PASSED"]
            pending_manual = [c["title"] for c in manual_list if c["status"] != "PASSED"]
            if not failing_auto and not pending_manual:
                verdict = "READY_FOR_LIVE_PILOT"
                verdict_label = "Ready for Live Pilot"
                verdict_summary = (
                    f"All {total_checks} quantitative and operational checks passed across "
                    f"{total_trades} trades / {trading_days} days. Eligible for bounded Live Pilot promotion."
                )
            elif not failing_auto and pending_manual:
                verdict = "NEEDS_SAFETY_SIGNOFF"
                verdict_label = "Edge Qualified — Complete Safety Checks"
                verdict_summary = (
                    f"Quantitative edge passed ({expectancy_r:+.3f}R, PF {profit_factor:.2f}), "
                    f"but {len(pending_manual)} operational safety check(s) remain unverified."
                )
            else:
                verdict = "NEEDS_TUNING"
                verdict_label = "Needs Tuning Before Live"
                verdict_summary = (
                    f"Sample size reached ({total_trades} trades), but failed: {', '.join(failing_auto)}. "
                    "Review Strategy Diagnostics on /analytics before promoting."
                )

        return {
            "verdict": verdict,
            "verdict_label": verdict_label,
            "verdict_summary": verdict_summary,
            "passed_count": total_passed,
            "total_checks": total_checks,
            "trades_progress": {
                "current": total_trades,
                "target": min_trades,
                "pct": round(min(100.0, (total_trades / max(1, min_trades)) * 100.0), 1),
            },
            "days_progress": {
                "current": trading_days,
                "target": min_days,
                "pct": round(min(100.0, (trading_days / max(1, min_days)) * 100.0), 1),
            },
            "auto_checks": auto_checks,
            "manual_checks": manual_list,
        }

    def compute_wealth_and_custody(
        self,
        broker_equity: float,
        config: Dict[str, Any],
        ledger: List[Dict[str, Any]],
        cum_metrics: Dict[str, Any],
        projections: Dict[str, Any],
        cumulative_roi_pct: float = 0.0,
    ) -> Dict[str, Any]:
        """Computes Seed -> Goal progress, Protected Vault vs Broker split, Sweep suggestions & ETA."""
        seed = max(0.0, float(config.get("seed_capital") or 10000.0))
        goal = max(seed + 1.0, float(config.get("target_capital_goal") or 50000.0))
        reserve_pct = max(0.0, min(90.0, float(config.get("reserve_pct") or 20.0)))
        max_live_cap = config.get("max_live_accessible_capital")
        max_live_cap_val = float(max_live_cap) if (max_live_cap is not None and float(max_live_cap) > 0) else None
        lifecycle_stage = str(config.get("lifecycle_stage") or "PAPER_QUALIFICATION")
        is_live_wealth_mode = (not settings.ALPACA_PAPER) or (lifecycle_stage in ("LIVE_PILOT", "LIVE_ACTIVE"))

        # Compute External Protected Vault balance and net external deposits from Capital Ledger
        vault_balance = 0.0
        total_deposits = 0.0
        total_withdrawals = 0.0
        for row in ledger:
            amt = float(row.get("amount") or 0.0)
            ftype = str(row.get("flow_type") or "")
            direction = str(row.get("direction") or "")
            if ftype == "VAULT_SWEEP" or direction == "OUT_TO_VAULT":
                vault_balance += amt
            elif ftype == "VAULT_FUNDING":
                vault_balance = max(0.0, vault_balance - amt)
            elif ftype in ("INITIAL_SEED", "DEPOSIT"):
                total_deposits += amt
            elif ftype == "WITHDRAWAL":
                total_withdrawals += amt

        vault_balance = round(vault_balance, 2)
        broker_eq = round(max(0.0, float(broker_equity)), 2)

        if is_live_wealth_mode:
            total_wealth = round(broker_eq + vault_balance, 2)
        else:
            # Paper preview mode: project cumulative TWR % onto configured Seed Capital
            # so a $100k Alpaca paper account doesn't distort a smaller live seed->goal bar
            total_wealth = round(max(0.0, seed * (1.0 + (float(cumulative_roi_pct) / 100.0))) + vault_balance, 2)

        # Progress from Seed -> Goal
        span = goal - seed
        gained = total_wealth - seed
        progress_pct = round(max(0.0, min(100.0, (gained / span) * 100.0)), 1) if span > 0 else 0.0
        remaining_to_goal = round(max(0.0, goal - total_wealth), 2)

        # Protected Vault target & Surplus Sweep suggestion (live mode only)
        target_vault = round(total_wealth * (reserve_pct / 100.0), 2)
        target_deployable = round(total_wealth - target_vault, 2)
        actual_vault_pct = round((vault_balance / total_wealth) * 100.0, 1) if total_wealth > 0 else 0.0

        sweep_suggestion = 0.0
        sweep_reason = ""
        if is_live_wealth_mode:
            if max_live_cap_val is not None and broker_eq > max_live_cap_val:
                sweep_suggestion = round(broker_eq - max_live_cap_val, 2)
                sweep_reason = (
                    f"Broker equity (${broker_eq:,.2f}) exceeds Maximum Live Accessible Capital "
                    f"(${max_live_cap_val:,.2f}). Sweep surplus to Protected Vault."
                )
            elif vault_balance < target_vault and (target_vault - vault_balance) >= max(250.0, total_wealth * 0.05):
                sweep_suggestion = round(target_vault - vault_balance, 2)
                sweep_reason = (
                    f"Protected Vault is {actual_vault_pct:.1f}% vs. {reserve_pct:.0f}% target. "
                    f"Consider sweeping ${sweep_suggestion:,.2f} from broker to vault."
                )

        # Geometric ETA to Goal using observed expectancy & trade frequency
        eta_trading_days: Optional[int] = None
        eta_months: Optional[float] = None
        exp_r = float(cum_metrics.get("expectancy_r", 0.0))
        total_trades = int(cum_metrics.get("total_trades", 0))
        avg_tpd = float(projections.get("avg_trades_per_day") or 1.0)
        base_risk_pct = self.resolve_base_risk_pct(config)

        if total_trades >= 5 and exp_r > 0.0 and total_wealth > 0 and total_wealth < goal:
            per_trade_growth = 1.0 + (base_risk_pct / 100.0) * exp_r
            if per_trade_growth > 1.0:
                trades_needed = math.log(goal / total_wealth) / math.log(per_trade_growth)
                eta_trading_days = max(1, int(math.ceil(trades_needed / max(0.25, avg_tpd))))
                eta_months = round(eta_trading_days / 21.0, 1)

        return {
            "is_live_wealth_mode": is_live_wealth_mode,
            "seed_capital": round(seed, 2),
            "target_capital_goal": round(goal, 2),
            "broker_equity": broker_eq,
            "external_vault_balance": vault_balance,
            "total_programme_wealth": total_wealth,
            "progress_pct": progress_pct,
            "remaining_to_goal": remaining_to_goal,
            "reserve_pct": reserve_pct,
            "actual_vault_pct": actual_vault_pct,
            "target_vault_balance": target_vault,
            "target_deployable_capital": target_deployable,
            "max_live_accessible_capital": max_live_cap_val,
            "ceiling_exceeded": bool(is_live_wealth_mode and max_live_cap_val is not None and broker_eq > max_live_cap_val),
            "surplus_sweep_suggestion": sweep_suggestion,
            "surplus_sweep_reason": sweep_reason,
            "total_deposits": round(total_deposits, 2),
            "total_withdrawals": round(total_withdrawals, 2),
            "eta_trading_days": eta_trading_days,
            "eta_months": eta_months,
        }

    async def build_financing_summary(self, broker_equity: Optional[float] = None) -> Dict[str, Any]:
        """Builds the complete Financing, Qualification & Wealth-Building payload for /financing."""
        report = await analytics_service.build_comprehensive_report()
        roi = report.get("roi_equity", {})
        cum = report.get("cumulative_trades_metrics", {})
        projections = report.get("projections", {})

        eq = float(broker_equity) if broker_equity is not None else float(roi.get("current_equity") or 0.0)
        controls = await self.get_effective_risk_controls(eq)
        config = await get_financing_config()
        ledger = await get_capital_ledger(limit=200)
        manual_checks = await get_validation_checks()
        _, brake_reason = await self.is_brake_latched()

        async with get_connection() as conn:
            row = await (await conn.execute("SELECT COUNT(*) AS cnt FROM trades WHERE is_open = 1;")).fetchone()
            open_trades_count = int(row["cnt"]) if row else 0

        qualification = self.evaluate_qualification(
            config=config,
            report=report,
            manual_checks=manual_checks,
            open_trades_count=open_trades_count,
        )
        wealth = self.compute_wealth_and_custody(
            broker_equity=eq,
            config=config,
            ledger=ledger,
            cum_metrics=cum,
            projections=projections,
            cumulative_roi_pct=float(roi.get("cumulative_roi_pct") or 0.0),
        )

        # Realised trading P&L summary (strictly separate from capital_ledger flows)
        realised_summary = {
            "total_closed_trades": int(cum.get("total_trades", 0)),
            "gross_profit": float(cum.get("gross_profit", 0.0)),
            "gross_loss": float(cum.get("gross_loss", 0.0)),
            "net_realised_pnl": float(cum.get("net_pnl", 0.0)),
        }

        return {
            "config": config,
            "risk_profiles": RISK_PROFILES,
            "controls": {
                "allowed": controls.allowed,
                "block_reason": controls.block_reason,
                "effective_equity": round(controls.effective_equity, 2),
                "base_risk_pct": controls.base_risk_pct,
                "health_state": controls.health_state,
                "health_multiplier": controls.health_multiplier,
                "pilot_multiplier": controls.pilot_multiplier,
                "combined_risk_multiplier": controls.combined_risk_multiplier,
                "effective_risk_pct": controls.effective_risk_pct,
                "current_drawdown_pct": controls.current_drawdown_pct,
                "lifecycle_stage": controls.lifecycle_stage,
                "enforcement_enabled": controls.enforcement_enabled,
                "brake_latched": controls.brake_latched,
                "brake_reason": brake_reason,
            },
            "qualification": qualification,
            "wealth": wealth,
            "realised_tax_pnl": realised_summary,
            "ledger": ledger,
            "is_paper_broker": bool(settings.ALPACA_PAPER),
        }

    async def build_tax_pnl_csv(self) -> str:
        """Generates a realised trading P&L CSV strictly excluding deposits, withdrawals, and vault sweeps."""
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, COALESCE(trade_date, substr(entry_time, 1, 10)) AS trade_date,
                       symbol, qty, entry_time, exit_time, entry_price, exit_price,
                       pnl_dollars, pnl_r, exit_reason
                FROM trades
                WHERE is_open = 0
                ORDER BY COALESCE(exit_time, entry_time) ASC, id ASC;
                """
            )
            rows = [dict(r) for r in await cursor.fetchall()]

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "trade_id",
                "trade_date",
                "symbol",
                "qty",
                "entry_time",
                "exit_time",
                "entry_price",
                "exit_price",
                "realised_pnl_usd",
                "pnl_r",
                "exit_reason",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r["id"],
                    r["trade_date"] or "",
                    r["symbol"],
                    r["qty"],
                    r["entry_time"] or "",
                    r["exit_time"] or "",
                    r["entry_price"],
                    r["exit_price"],
                    r["pnl_dollars"],
                    r["pnl_r"],
                    r["exit_reason"] or "",
                ]
            )
        return buf.getvalue()


financing_service = FinancingService()

"""Event-driven portfolio simulator mirroring RiskManager, FinancingService, and LifecycleWorker.

Models:
- Dynamic compounding risk sizing (P1/P2/P3/CUSTOM)
- Risk-free breakeven slot recycling (4 at-risk / 6 total positions)
- Drawdown Health Ladder via live FinancingService.compute_health_state()
- Daily -2.0% circuit breaker
- Marketable-limit fills (close * 1.0005) with 3-minute stale order expiry
- +1.0R breakeven stop stepping to entry + $0.02 with conservative SL-first intrabar tie-breaking
- Configurable exit slippage, FOMC pre-event flatten, and early-close EOD flatten
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import floor
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.financing_service import FinancingService
from backtest.config import BacktestConfig
from backtest.data_loader import Bar


@dataclass
class SimPendingOrder:
    symbol: str
    qty: int
    signal_time: datetime
    expires_at: datetime
    signal_price: float
    entry_limit_price: float
    initial_stop_price: float
    take_profit_price: float
    stop_type: str
    risk_dollars: float
    stop_distance: float
    signal_rvol: float
    signal_atr_ext: float
    or_high: float
    or_low: float
    or_width_pct: float
    cm_tech_rating: Optional[float]
    cm_setup_rating: Optional[float]
    cm_rs: Optional[float]
    cm_fundamental_rating: Optional[float]
    cm_sector: Optional[str]
    health_state: str
    risk_multiplier: float


@dataclass
class SimPosition:
    symbol: str
    qty: int
    trade_date: str
    signal_time: datetime
    entry_time: datetime
    signal_price: float
    entry_limit_price: float
    entry_price: float
    initial_stop_price: float
    active_stop_price: float
    take_profit_price: float
    initial_risk_dollars: float
    risk_1r: float
    stop_type: str
    signal_rvol: float
    signal_atr_ext: float
    or_high: float
    or_low: float
    or_width_pct: float
    cm_tech_rating: Optional[float]
    cm_setup_rating: Optional[float]
    cm_rs: Optional[float]
    cm_fundamental_rating: Optional[float]
    cm_sector: Optional[str]
    health_state: str
    risk_multiplier: float
    breakeven_stepped: bool = False
    last_price: float = 0.0
    max_high_seen: float = 0.0
    min_low_seen: float = 0.0


class SimulatedPortfolioBroker:
    """In-memory portfolio broker enforcing live pTrading risk & lifecycle rules chronologically."""

    def __init__(self, cfg: BacktestConfig) -> None:
        self.cfg = cfg
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)

        self.cash: float = float(cfg.initial_capital)
        self.starting_day_equity: float = float(cfg.initial_capital)
        self.circuit_breaker_tripped: bool = False

        # Cumulative TWR tracking for Health-State Drawdown Ladder
        self.twr_growth: float = 1.0
        self.twr_peak: float = 1.0
        self.health_state: str = "HEALTHY"
        self.health_latched: bool = False

        self.open_positions: Dict[str, SimPosition] = {}
        self.pending_orders: Dict[str, SimPendingOrder] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.today_closed_trades: List[Dict[str, Any]] = []

        # Signal funnel & diagnostics counters
        self.total_signals: int = 0
        self.approved_signals: int = 0
        self.rejected_signals: int = 0
        self.expired_orders: int = 0
        self.rejection_reasons: Dict[str, int] = {}
        self.circuit_breaker_days: int = 0
        self.fomc_flatten_count: int = 0

    def current_equity(self) -> float:
        pos_val = sum(p.qty * p.last_price for p in self.open_positions.values())
        return round(self.cash + pos_val, 2)

    def current_buying_power(self) -> float:
        """Models Alpaca Reg-T intraday buying power (2x equity minus active market value, floored at cash)."""
        eq = self.current_equity()
        pos_val = sum(p.qty * p.last_price for p in self.open_positions.values())
        pending_val = sum(o.qty * o.entry_limit_price for o in self.pending_orders.values())
        # Intraday margin buying power is 2x equity for margin accounts >= $25k, else 1x cash
        multiplier = 2.0 if eq >= 25000.0 else 1.0
        return max(0.0, (eq * multiplier) - pos_val - pending_val)

    def current_drawdown_pct(self) -> float:
        eq = self.current_equity()
        day_ret = ((eq - self.starting_day_equity) / self.starting_day_equity) if self.starting_day_equity > 0 else 0.0
        live_twr = self.twr_growth * (1.0 + day_ret)
        peak = max(self.twr_peak, live_twr)
        if peak <= 0:
            return 0.0
        return round(max(0.0, ((peak - live_twr) / peak) * 100.0), 2)

    def start_new_day(self) -> None:
        self.starting_day_equity = self.current_equity()
        self.circuit_breaker_tripped = False
        self.today_closed_trades = []

    def finalize_day(self) -> Tuple[float, float, float]:
        """Updates TWR growth & peak at end of day. Returns (ending_equity, daily_roi_pct, dd_pct)."""
        end_eq = self.current_equity()
        ret = ((end_eq - self.starting_day_equity) / self.starting_day_equity) if self.starting_day_equity > 0 else 0.0
        self.twr_growth *= (1.0 + ret)
        if self.twr_growth > self.twr_peak:
            self.twr_peak = self.twr_growth
        dd_pct = round(max(0.0, ((self.twr_peak - self.twr_growth) / self.twr_peak) * 100.0), 2) if self.twr_peak > 0 else 0.0
        if self.cfg.enable_health_ladder:
            self.health_state, self.health_latched, _ = FinancingService.compute_health_state(
                current_dd_pct=dd_pct,
                previous_state=self.health_state,
                latched=self.health_latched,
                brake_latched=False,
            )
        return end_eq, round(ret * 100.0, 4), dd_pct

    def _reject(self, reason_key: str) -> Tuple[bool, str]:
        self.rejected_signals += 1
        self.rejection_reasons[reason_key] = self.rejection_reasons.get(reason_key, 0) + 1
        return False, reason_key

    def evaluate_and_submit(
        self,
        *,
        symbol: str,
        signal_time: datetime,
        close_price: float,
        or_high: float,
        or_low: float,
        atr: float,
        signal_rvol: float,
        cm_tech_rating: Optional[float] = None,
        cm_setup_rating: Optional[float] = None,
        cm_rs: Optional[float] = None,
        cm_fundamental_rating: Optional[float] = None,
        cm_sector: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Mirrors RiskManager.evaluate_signal() and queues a marketable-limit bracket order."""
        self.total_signals += 1
        sym = symbol.upper()
        equity = self.current_equity()

        # 1. Daily -2.0% Circuit Breaker
        if self.circuit_breaker_tripped:
            return self._reject("Daily circuit breaker active")

        daily_dd_pct = ((equity - self.starting_day_equity) / self.starting_day_equity) * 100.0 if self.starting_day_equity > 0 else 0.0
        if daily_dd_pct <= -self.cfg.daily_max_loss_percent:
            self.circuit_breaker_tripped = True
            self.circuit_breaker_days += 1
            return self._reject("Daily circuit breaker tripped")

        # 2. Financing Health-State Drawdown Ladder (reusing live FinancingService.compute_health_state)
        health_state = "HEALTHY"
        health_mult = 1.0
        if self.cfg.enable_health_ladder:
            dd_pct = self.current_drawdown_pct()
            health_state, self.health_latched, health_mult = FinancingService.compute_health_state(
                current_dd_pct=dd_pct,
                previous_state=self.health_state,
                latched=self.health_latched,
                brake_latched=False,
            )
            self.health_state = health_state
            if health_state in ("SUSPENDED", "CAPITAL_PROTECTION") or health_mult <= 0.0:
                return self._reject(f"Health ladder blocked ({health_state})")

        # 3. Duplicate position / pending order checks
        if sym in self.open_positions:
            return self._reject("Already holding symbol")
        if sym in self.pending_orders:
            return self._reject("Pending entry already exists")

        # 4. Concurrent Position Caps & Risk-Free Breakeven Slot Recycling (4 at-risk / 6 total)
        pending_count = len(self.pending_orders)
        total_positions = len(self.open_positions) + pending_count

        if self.cfg.enable_slot_recycling:
            at_risk_count = sum(1 for p in self.open_positions.values() if not p.breakeven_stepped) + pending_count
            if at_risk_count >= self.cfg.max_concurrent_positions:
                return self._reject("Max concurrent at-risk positions cap")
            if total_positions >= self.cfg.max_total_positions_with_breakeven:
                return self._reject("Max concurrent total positions cap (incl. BE)")
        else:
            if total_positions >= self.cfg.max_concurrent_positions:
                return self._reject("Max concurrent positions cap")

        # 5. Entry, Stop-Loss, and Sizing Math (exact RiskManager formulas by default)
        entry_limit_price = round(close_price * (1.0 + self.cfg.entry_limit_buffer_pct / 100.0), 2)
        if self.cfg.use_pure_or_low_stop:
            stop_loss_price = round(or_low, 2)
            stop_type = "OR_LOW"
        else:
            atr_stop = round(close_price - self.cfg.stop_atr_multiplier * atr, 2)
            stop_loss_price = round(max(or_low, atr_stop), 2)
            stop_type = "OR_LOW" if or_low >= atr_stop else f"ATR_{str(self.cfg.stop_atr_multiplier).replace('.', '_')}"
        stop_distance = round(entry_limit_price - stop_loss_price, 2)

        if stop_distance < self.cfg.min_stop_distance_dollars:
            return self._reject("Stop distance too narrow")

        effective_risk_pct = self.cfg.resolved_base_risk_pct() * health_mult
        risk_dollars = round(equity * (effective_risk_pct / 100.0), 2)
        raw_shares = floor(risk_dollars / stop_distance)

        max_capital = equity * (self.cfg.max_equity_percent_per_stock / 100.0)
        max_shares = floor(max_capital / entry_limit_price) if entry_limit_price > 0 else 0
        shares = min(raw_shares, max_shares)

        bp = self.current_buying_power()
        if bp > 0 and entry_limit_price > 0:
            max_bp_shares = floor((bp * 0.98) / entry_limit_price)
            shares = min(shares, max_bp_shares)
        else:
            shares = 0

        if shares <= 0:
            return self._reject("0 shares under capital/buying-power cap")

        take_profit_price = round(entry_limit_price + (self.cfg.take_profit_r * stop_distance), 2)
        atr_ext = round((close_price - or_high) / atr, 4) if atr > 0 else 0.0
        or_width_pct = round(((or_high - or_low) / close_price) * 100.0, 4) if close_price > 0 else 0.0

        expires_at = signal_time + timedelta(minutes=self.cfg.stale_order_timeout_minutes)

        self.pending_orders[sym] = SimPendingOrder(
            symbol=sym,
            qty=shares,
            signal_time=signal_time,
            expires_at=expires_at,
            signal_price=round(close_price, 4),
            entry_limit_price=entry_limit_price,
            initial_stop_price=stop_loss_price,
            take_profit_price=take_profit_price,
            stop_type=stop_type,
            risk_dollars=risk_dollars,
            stop_distance=stop_distance,
            signal_rvol=round(signal_rvol, 3),
            signal_atr_ext=atr_ext,
            or_high=round(or_high, 2),
            or_low=round(or_low, 2),
            or_width_pct=or_width_pct,
            cm_tech_rating=cm_tech_rating,
            cm_setup_rating=cm_setup_rating,
            cm_rs=cm_rs,
            cm_fundamental_rating=cm_fundamental_rating,
            cm_sector=cm_sector,
            health_state=health_state,
            risk_multiplier=health_mult,
        )
        self.approved_signals += 1
        return True, None

    def _apply_exit_slippage(self, price: float) -> float:
        if self.cfg.exit_slippage_bps <= 0:
            return round(price, 4)
        return round(price * (1.0 - self.cfg.exit_slippage_bps / 10000.0), 4)

    def _close_position(
        self,
        pos: SimPosition,
        exit_time: datetime,
        exit_price: float,
        exit_reason: str,
    ) -> None:
        commission = self.cfg.commission_per_share * pos.qty * 2.0
        proceeds = (pos.qty * exit_price) - (self.cfg.commission_per_share * pos.qty)
        self.cash = round(self.cash + proceeds, 4)

        pnl_dollars = round(((exit_price - pos.entry_price) * pos.qty) - commission, 2)
        risk_1r = max(pos.entry_price - pos.initial_stop_price, 0.0001)
        initial_risk_dollars = round(risk_1r * pos.qty, 2)
        pnl_r = round(pnl_dollars / initial_risk_dollars, 4) if initial_risk_dollars > 0 else 0.0

        slippage_dollars = round(pos.entry_price - pos.signal_price, 4)
        slippage_r = round(slippage_dollars / risk_1r, 4) if risk_1r > 0 else 0.0

        mfe_r = round(max(0.0, (pos.max_high_seen - pos.entry_price) / risk_1r), 3)
        mae_r = round(max(0.0, (pos.entry_price - pos.min_low_seen) / risk_1r), 3)

        trade_rec: Dict[str, Any] = {
            "trade_date": pos.trade_date,
            "symbol": pos.symbol,
            "qty": pos.qty,
            "signal_time": pos.signal_time.isoformat(),
            "entry_time": pos.entry_time.isoformat(),
            "exit_time": exit_time.isoformat(),
            "signal_price": pos.signal_price,
            "entry_limit_price": pos.entry_limit_price,
            "entry_price": pos.entry_price,
            "initial_stop_price": pos.initial_stop_price,
            "stop_loss_price": pos.active_stop_price,
            "take_profit_price": pos.take_profit_price,
            "exit_price": round(exit_price, 4),
            "initial_risk_dollars": initial_risk_dollars,
            "pnl_dollars": pnl_dollars,
            "pnl_r": pnl_r,
            "slippage_dollars": slippage_dollars,
            "slippage_r": slippage_r,
            "stop_type": pos.stop_type,
            "exit_reason": exit_reason,
            "breakeven_stepped": 1 if pos.breakeven_stepped else 0,
            "mfe_r": mfe_r,
            "mae_r": mae_r,
            "post_exit_max_r": None,
            "signal_rvol": pos.signal_rvol,
            "signal_atr_ext": pos.signal_atr_ext,
            "or_high": pos.or_high,
            "or_low": pos.or_low,
            "or_width_pct": pos.or_width_pct,
            "cm_tech_rating": pos.cm_tech_rating,
            "cm_setup_rating": pos.cm_setup_rating,
            "cm_rs": pos.cm_rs,
            "cm_fundamental_rating": pos.cm_fundamental_rating,
            "cm_sector": pos.cm_sector,
            "health_state": pos.health_state,
            "risk_multiplier": pos.risk_multiplier,
            "is_open": 0,
        }
        self.closed_trades.append(trade_rec)
        self.today_closed_trades.append(trade_rec)
        self.open_positions.pop(pos.symbol, None)

        # Immediately check if realized loss tripped the -2.0% daily circuit breaker
        eq = self.current_equity()
        dd = ((eq - self.starting_day_equity) / self.starting_day_equity) * 100.0 if self.starting_day_equity > 0 else 0.0
        if not self.circuit_breaker_tripped and dd <= -self.cfg.daily_max_loss_percent:
            self.circuit_breaker_tripped = True
            self.circuit_breaker_days += 1

    def step_execution_bars(self, bar_ts: datetime, bars_at_ts: Dict[str, Bar]) -> None:
        """Processes one chronological execution bar step (1m or 5m) for fills, expiries, BE steps, and exits."""
        # 1. Process pending entry orders (expire or fill)
        for sym in list(self.pending_orders.keys()):
            order = self.pending_orders[sym]
            if bar_ts > order.expires_at:
                del self.pending_orders[sym]
                self.expired_orders += 1
                continue

            bar = bars_at_ts.get(sym)
            if bar is None:
                continue

            # Marketable limit buy fills if market trades at or below limit price
            if bar.low <= order.entry_limit_price:
                fill_price = round(min(order.entry_limit_price, bar.open) if bar.open <= order.entry_limit_price else order.entry_limit_price, 4)
                cost = (order.qty * fill_price) + (self.cfg.commission_per_share * order.qty)
                self.cash = round(self.cash - cost, 4)

                # Recompute 1R from actual fill price (mirrors LifecycleWorker)
                risk_1r = max(fill_price - order.initial_stop_price, 0.01)
                trade_date_str = bar_ts.astimezone(self.tz).strftime("%Y-%m-%d")

                pos = SimPosition(
                    symbol=sym,
                    qty=order.qty,
                    trade_date=trade_date_str,
                    signal_time=order.signal_time,
                    entry_time=bar_ts,
                    signal_price=order.signal_price,
                    entry_limit_price=order.entry_limit_price,
                    entry_price=fill_price,
                    initial_stop_price=order.initial_stop_price,
                    active_stop_price=order.initial_stop_price,
                    take_profit_price=order.take_profit_price,
                    initial_risk_dollars=round(risk_1r * order.qty, 2),
                    risk_1r=risk_1r,
                    stop_type=order.stop_type,
                    signal_rvol=order.signal_rvol,
                    signal_atr_ext=order.signal_atr_ext,
                    or_high=order.or_high,
                    or_low=order.or_low,
                    or_width_pct=order.or_width_pct,
                    cm_tech_rating=order.cm_tech_rating,
                    cm_setup_rating=order.cm_setup_rating,
                    cm_rs=order.cm_rs,
                    cm_fundamental_rating=order.cm_fundamental_rating,
                    cm_sector=order.cm_sector,
                    health_state=order.health_state,
                    risk_multiplier=order.risk_multiplier,
                    breakeven_stepped=False,
                    last_price=bar.close,
                    max_high_seen=max(fill_price, bar.high),
                    min_low_seen=min(fill_price, bar.low),
                )
                del self.pending_orders[sym]
                self.open_positions[sym] = pos
                # Evaluate conservative stop/BE/TP on entry bar itself
                self._evaluate_open_position_bar(pos, bar_ts, bar, just_entered=True)

        # 2. Process all pre-existing open positions
        for sym in list(self.open_positions.keys()):
            pos = self.open_positions.get(sym)
            if pos is None or pos.entry_time == bar_ts:
                continue
            bar = bars_at_ts.get(sym)
            if bar is None:
                continue
            self._evaluate_open_position_bar(pos, bar_ts, bar, just_entered=False)

    def _evaluate_open_position_bar(
        self,
        pos: SimPosition,
        bar_ts: datetime,
        bar: Bar,
        just_entered: bool,
    ) -> None:
        pos.last_price = bar.close
        if bar.high > pos.max_high_seen:
            pos.max_high_seen = bar.high
        if bar.low < pos.min_low_seen:
            pos.min_low_seen = bar.low

        # Conservative intrabar rule: ALWAYS check active stop FIRST before assuming TP or new BE step
        if bar.low <= pos.active_stop_price:
            raw_exit = min(bar.open, pos.active_stop_price) if (not just_entered and bar.open <= pos.active_stop_price) else pos.active_stop_price
            exit_px = self._apply_exit_slippage(raw_exit)
            reason = "BREAKEVEN" if pos.breakeven_stepped else "STOP_LOSS"
            self._close_position(pos, bar_ts, exit_px, reason)
            return

        # Step stop to Breakeven (+$0.02) when bar high reaches +1.0R
        be_target = pos.entry_price + (self.cfg.breakeven_trigger_r * pos.risk_1r)
        if not pos.breakeven_stepped and bar.high >= be_target:
            pos.active_stop_price = round(pos.entry_price + self.cfg.breakeven_offset_dollars, 2)
            pos.breakeven_stepped = True

        # Check Take-Profit limit (+2.0R)
        if bar.high >= pos.take_profit_price:
            self._close_position(pos, bar_ts, pos.take_profit_price, "TAKE_PROFIT")
            return

    def flatten_all(
        self,
        ts: datetime,
        last_prices: Dict[str, float],
        reason: str = "EOD_FLATTEN",
    ) -> int:
        """Cancels pending orders and closes all open positions (EOD or FOMC pre-event)."""
        if self.pending_orders:
            self.expired_orders += len(self.pending_orders)
            self.pending_orders.clear()

        count = len(self.open_positions)
        if reason == "FOMC_FLATTEN" and count > 0:
            self.fomc_flatten_count += count

        for sym in list(self.open_positions.keys()):
            pos = self.open_positions[sym]
            px = last_prices.get(sym, pos.last_price)
            exit_px = self._apply_exit_slippage(px)
            self._close_position(pos, ts, exit_px, reason)
        return count

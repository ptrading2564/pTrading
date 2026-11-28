"""Chronological event-driven portfolio backtest orchestrator for pTrading.

Features month-scoped smart intraday bar loading (drastically lowering RAM & API calls
on multi-year Mode A backtests), optional experiment filters (custom entry cutoff,
midday blackout, SPY daily EMA20 trend filter), and 100% read-only reuse of live
MarketScanner, ChartMillImporter, FinancingService, AnalyticsService, and StrategyReviewer.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.market_scanner import MarketScanner
from backtest.analytics_bridge import build_backtest_report_and_review
from backtest.calendar_data import is_fomc_day_historical
from backtest.config import BacktestConfig
from backtest.data_loader import AlpacaCachedDataStore, Bar, HistoricalDataStore
from backtest.db import (
    create_backtest_run,
    fail_backtest_run,
    save_backtest_result,
    update_backtest_progress,
)
from backtest.edgar_fundamentals import EdgarFundamentalProvider
from backtest.screener_sim import ScreenedCandidate, ScreenerSimulator
from backtest.sim_broker import SimulatedPortfolioBroker
from backtest.universe import get_liquid_us_universe

logger = logging.getLogger(__name__)

AsyncProgressHook = Optional[Callable[[float, str], Any]]


def _parse_hhmm(val: str, default: Tuple[int, int]) -> time:
    try:
        h, m = map(int, val.strip()[:5].split(":"))
        return time(h, m)
    except Exception:
        return time(default[0], default[1])


def _classify_market_regime(spy_ret_pct: Optional[float], qqq_ret_pct: Optional[float]) -> str:
    if spy_ret_pct is None:
        return "UNKNOWN"
    avg_r = spy_ret_pct if qqq_ret_pct is None else (spy_ret_pct + qqq_ret_pct) / 2.0
    if avg_r >= 0.35:
        return "TREND_UP"
    if avg_r <= -0.35:
        return "TREND_DOWN"
    return "CHOP"


def _compute_spy_above_ema20_by_date(spy_daily_bars: List[Bar], tz: ZoneInfo) -> Dict[str, bool]:
    """Point-in-time SPY daily trend flag: for day T, checks if day T-1 close > EMA(20) (zero look-ahead)."""
    sorted_b = sorted(spy_daily_bars, key=lambda b: b.timestamp)
    if len(sorted_b) < 21:
        return {}
    alpha = 2.0 / (20.0 + 1.0)
    ema = sum(b.close for b in sorted_b[:20]) / 20.0
    # Compute status after each bar i, then apply strictly to bar i+1's date!
    status_after_bar: List[Tuple[str, bool]] = []
    for i in range(20, len(sorted_b)):
        c = sorted_b[i].close
        ema = (c * alpha) + (ema * (1.0 - alpha))
        d_str = sorted_b[i].timestamp.astimezone(tz).strftime("%Y-%m-%d")
        status_after_bar.append((d_str, c >= ema))

    out: Dict[str, bool] = {}
    for i in range(1, len(status_after_bar)):
        # Day T uses strictly day T-1's close vs EMA20
        out[status_after_bar[i][0]] = status_after_bar[i - 1][1]
    return out


class BacktestEngine:
    """Runs deterministic multi-symbol portfolio backtests matching live pTrading rules."""

    def __init__(
        self,
        cfg: BacktestConfig,
        data_store: Optional[HistoricalDataStore] = None,
    ) -> None:
        self.cfg = cfg
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)
        self.data_store: HistoricalDataStore = data_store or AlpacaCachedDataStore()

    async def run(
        self,
        run_id: Optional[str] = None,
        persist: bool = True,
        progress_cb: AsyncProgressHook = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Dict[str, Any]:
        """Executes the full backtest and optionally persists results to data/backtest.db."""
        start_d = date.fromisoformat(self.cfg.start_date)
        end_d = date.fromisoformat(self.cfg.end_date)
        if end_d < start_d:
            raise ValueError("end_date must be on or after start_date.")

        run_name = self.cfg.name.strip() or (
            f"{self.cfg.mode.upper()} ({self.cfg.feed.upper()}) {self.cfg.start_date}..{self.cfg.end_date}"
        )

        if persist and run_id is None:
            run_id = await create_backtest_run(
                name=run_name,
                mode=self.cfg.mode,
                feed=self.cfg.feed,
                start_date=self.cfg.start_date,
                end_date=self.cfg.end_date,
                config_dict=self.cfg.to_dict(),
            )

        async def _report_progress(pct: float, msg: str) -> None:
            if persist and run_id:
                await update_backtest_progress(run_id, "RUNNING", pct, msg)
            if progress_cb:
                res = progress_cb(pct, msg)
                if asyncio.iscoroutine(res):
                    await res

        def _check_cancel() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError("Backtest cancelled by user.")

        try:
            await _report_progress(2.0, "Loading NYSE trading calendar...")
            sessions_map = await self.data_store.get_trading_sessions(start_d, end_d)
            trading_dates = sorted(sessions_map.keys())
            if not trading_dates:
                raise RuntimeError(f"No NYSE trading sessions found between {self.cfg.start_date} and {self.cfg.end_date}.")

            _check_cancel()

            # Step 1: Load 1Day bars for screener & SPY/QQQ daily returns/EMA20
            daily_lookback_start = datetime.combine(start_d - timedelta(days=400), time(0, 0), tzinfo=self.tz)
            daily_end = datetime.combine(end_d + timedelta(days=1), time(23, 59), tzinfo=self.tz)

            if self.cfg.mode.lower() == "screener":
                daily_symbols = list(dict.fromkeys(["SPY", "QQQ", *get_liquid_us_universe()]))
            else:
                daily_symbols = list(dict.fromkeys(["SPY", "QQQ", *self.cfg.resolved_fixed_symbols()]))

            await _report_progress(5.0, f"Loading 1Day history for {len(daily_symbols)} symbols...")
            daily_bars = await self.data_store.get_bars(
                symbols=daily_symbols,
                timeframe="1Day",
                start_dt=daily_lookback_start,
                end_dt=daily_end,
                feed=self.cfg.feed,
            )

            _check_cancel()

            # Step 2: Build point-in-time daily baskets across all trading days
            await _report_progress(14.0, "Running point-in-time daily pre-market screener...")
            edgar = EdgarFundamentalProvider() if self.cfg.enable_edgar_fundamentals else None
            screener = ScreenerSimulator(self.cfg, daily_bars, edgar_provider=edgar)

            baskets_by_day: Dict[str, List[ScreenedCandidate]] = {}
            for d_str in trading_dates:
                baskets_by_day[d_str] = screener.screen_for_date(date.fromisoformat(d_str))

            daily_ret_spy = self._compute_daily_returns_map(daily_bars.get("SPY", []))
            daily_ret_qqq = self._compute_daily_returns_map(daily_bars.get("QQQ", []))
            spy_above_ema20 = _compute_spy_above_ema20_by_date(daily_bars.get("SPY", []), self.tz)

            # Group trading dates by YYYY-MM for memory-efficient month-by-month intraday loading & replay
            months_ordered: List[str] = []
            dates_by_month: Dict[str, List[str]] = {}
            for d_str in trading_dates:
                ym = d_str[:7]
                if ym not in dates_by_month:
                    months_ordered.append(ym)
                    dates_by_month[ym] = []
                dates_by_month[ym].append(d_str)

            broker = SimulatedPortfolioBroker(self.cfg)
            daily_equity_rows: List[Dict[str, Any]] = []
            rolling_5m_history: Dict[str, List[Bar]] = {}
            n_months = max(1, len(months_ordered))

            for m_idx, ym in enumerate(months_ordered):
                _check_cancel()
                m_dates = dates_by_month[ym]
                m_start_d = date.fromisoformat(m_dates[0])
                m_end_d = date.fromisoformat(m_dates[-1])

                month_syms = sorted(
                    {"SPY", "QQQ"}
                    | {cand.symbol for d_s in m_dates for cand in baskets_by_day.get(d_s, [])}
                )
                base_pct = 16.0 + (m_idx / n_months) * 76.0

                await _report_progress(
                    base_pct,
                    f"Month {ym} ({m_idx + 1}/{n_months}): Loading 5Min bars ({len(month_syms)} active syms)...",
                )
                intra_start_5m = datetime.combine(m_start_d - timedelta(days=5), time(9, 30), tzinfo=self.tz)
                intra_end = datetime.combine(m_end_d, time(16, 5), tzinfo=self.tz)

                bars_5m_month = await self.data_store.get_bars(
                    symbols=month_syms,
                    timeframe="5Min",
                    start_dt=intra_start_5m,
                    end_dt=intra_end,
                    feed=self.cfg.feed,
                )

                _check_cancel()

                # Seed rolling 5m warmup history for any symbol not yet seeded
                m_start_str = m_dates[0]
                for sym, sym_bars in bars_5m_month.items():
                    if sym not in rolling_5m_history:
                        warmup = [b for b in sym_bars if b.timestamp.astimezone(self.tz).strftime("%Y-%m-%d") < m_start_str]
                        if warmup:
                            rolling_5m_history[sym] = warmup[-40:]

                bars_5m_by_day_sym = self._index_bars_by_day(bars_5m_month)

                # Smart 1Min fetch: only fetch 1Min bars for symbols that have >=1 potential ORB breakout bar this month!
                bars_1m_by_day_sym: Dict[str, Dict[str, List[Bar]]] = {}
                if self.cfg.use_1m_execution:
                    cand_1m_syms = self._identify_breakout_candidate_symbols(
                        m_dates, baskets_by_day, bars_5m_by_day_sym
                    )
                    if cand_1m_syms:
                        await _report_progress(
                            base_pct + (0.4 * (76.0 / n_months)),
                            f"Month {ym} ({m_idx + 1}/{n_months}): Loading 1Min bars ({len(cand_1m_syms)} breakout syms)...",
                        )
                        bars_1m_month = await self.data_store.get_bars(
                            symbols=cand_1m_syms,
                            timeframe="1Min",
                            start_dt=datetime.combine(m_start_d, time(9, 30), tzinfo=self.tz),
                            end_dt=intra_end,
                            feed=self.cfg.feed,
                        )
                        bars_1m_by_day_sym = self._index_bars_by_day(bars_1m_month)

                await _report_progress(
                    base_pct + (0.75 * (76.0 / n_months)),
                    f"Month {ym} ({m_idx + 1}/{n_months}): Simulating {len(m_dates)} sessions...",
                )

                for d_str in m_dates:
                    _check_cancel()
                    day_obj = date.fromisoformat(d_str)
                    sess = sessions_map[d_str]
                    basket = baskets_by_day.get(d_str, [])

                    # Optional higher-timeframe SPY > daily EMA20 filter
                    allow_entries_today = True
                    if self.cfg.require_spy_above_daily_ema20 and d_str in spy_above_ema20:
                        allow_entries_today = spy_above_ema20[d_str]

                    self._simulate_single_session(
                        day_obj=day_obj,
                        d_str=d_str,
                        sess=sess,
                        basket=basket if allow_entries_today else [],
                        bars_5m_today=bars_5m_by_day_sym.get(d_str, {}),
                        bars_1m_today=bars_1m_by_day_sym.get(d_str, {}),
                        rolling_5m_history=rolling_5m_history,
                        broker=broker,
                    )

                    end_eq, daily_roi_pct, dd_pct = broker.finalize_day()
                    spy_r = daily_ret_spy.get(d_str)
                    qqq_r = daily_ret_qqq.get(d_str)

                    daily_equity_rows.append(
                        {
                            "date": d_str,
                            "starting_equity": round(broker.starting_day_equity, 2),
                            "ending_equity": round(end_eq, 2),
                            "daily_pnl": round(end_eq - broker.starting_day_equity, 2),
                            "daily_roi_pct": round(daily_roi_pct, 4),
                            "drawdown_pct": round(dd_pct, 2),
                            "health_state": broker.health_state,
                            "trade_count": len(broker.today_closed_trades),
                            "circuit_breaker_tripped": broker.circuit_breaker_tripped,
                            "spy_return_pct": spy_r,
                            "qqq_return_pct": qqq_r,
                            "market_regime": _classify_market_regime(spy_r, qqq_r),
                            "basket_size": len(basket),
                        }
                    )

            # Step 5: Compute full AnalyticsService + StrategyReviewer report
            await _report_progress(94.0, "Running Monte Carlo Risk of Ruin & Deep-Dive Diagnostics...")
            funnel_stats = {
                "total_signals": broker.total_signals,
                "approved_signals": broker.approved_signals,
                "rejected_signals": broker.rejected_signals,
                "expired_stale_orders": broker.expired_orders,
                "rejection_reasons": broker.rejection_reasons,
                "circuit_breaker_days": broker.circuit_breaker_days,
                "fomc_flattened_positions": broker.fomc_flatten_count,
                "by_status": {
                    "APPROVED": broker.approved_signals,
                    "REJECTED": broker.rejected_signals,
                    "EXPIRED": broker.expired_orders,
                },
            }

            summary, report, review_md, review_source, diagnostics = await build_backtest_report_and_review(
                cfg=self.cfg,
                trades=broker.closed_trades,
                daily_rows=daily_equity_rows,
                funnel_stats=funnel_stats,
            )

            if persist and run_id:
                await save_backtest_result(
                    run_id=run_id,
                    summary=summary,
                    report=report,
                    trades=broker.closed_trades,
                    daily_rows=daily_equity_rows,
                    review_markdown=review_md,
                    review_source=review_source,
                    diagnostics=diagnostics,
                )

            return {
                "run_id": run_id,
                "name": run_name,
                "status": "COMPLETED",
                "config": self.cfg.to_dict(),
                "summary": summary,
                "report": report,
                "trades": broker.closed_trades,
                "daily_equity": daily_equity_rows,
                "review_markdown": review_md,
                "review_source": review_source,
                "diagnostics": diagnostics,
            }

        except asyncio.CancelledError:
            if persist and run_id:
                await update_backtest_progress(run_id, "CANCELLED", 0.0, "Cancelled by user")
            raise
        except Exception as e:
            logger.exception("Backtest run %s failed: %s", run_id, e)
            if persist and run_id:
                await fail_backtest_run(run_id, str(e))
            raise

    def _identify_breakout_candidate_symbols(
        self,
        m_dates: List[str],
        baskets_by_day: Dict[str, List[ScreenedCandidate]],
        bars_5m_by_day_sym: Dict[str, Dict[str, List[Bar]]],
    ) -> List[str]:
        """Pre-scans 5m bars for the month to find symbols that actually print at least one bullish close > OR_High."""
        candidates: set[str] = set()
        for d_str in m_dates:
            day_obj = date.fromisoformat(d_str)
            or_start = datetime.combine(day_obj, time(9, 30), tzinfo=self.tz)
            or_end = datetime.combine(day_obj, time(9, 45), tzinfo=self.tz)
            day_bars = bars_5m_by_day_sym.get(d_str, {})
            for cand in baskets_by_day.get(d_str, []):
                sym = cand.symbol
                if sym in candidates:
                    continue
                s_bars = day_bars.get(sym, [])
                if not s_bars:
                    continue
                or_b = [b for b in s_bars if or_start <= b.timestamp.astimezone(self.tz) < or_end]
                if not or_b:
                    continue
                or_h = round(max(b.high for b in or_b), 2)
                for b in s_bars:
                    if b.timestamp.astimezone(self.tz) >= or_end and b.close > or_h and b.close > b.open:
                        candidates.add(sym)
                        break
        return sorted(candidates)

    def _index_bars_by_day(self, bars_all: Dict[str, List[Bar]]) -> Dict[str, Dict[str, List[Bar]]]:
        by_day: Dict[str, Dict[str, List[Bar]]] = {}
        rth_open = time(9, 30)
        rth_close = time(16, 0)
        for sym, sym_bars in bars_all.items():
            for b in sym_bars:
                dt_et = b.timestamp.astimezone(self.tz)
                t_et = dt_et.time()
                if t_et < rth_open or t_et >= rth_close:
                    continue
                d_str = dt_et.strftime("%Y-%m-%d")
                by_day.setdefault(d_str, {}).setdefault(sym, []).append(b)
        return by_day

    def _compute_daily_returns_map(self, daily_bars: List[Bar]) -> Dict[str, float]:
        sorted_b = sorted(daily_bars, key=lambda b: b.timestamp)
        out: Dict[str, float] = {}
        for i in range(1, len(sorted_b)):
            prev_c = sorted_b[i - 1].close
            cur_c = sorted_b[i].close
            if prev_c > 0:
                d_str = sorted_b[i].timestamp.astimezone(self.tz).strftime("%Y-%m-%d")
                out[d_str] = round(((cur_c - prev_c) / prev_c) * 100.0, 3)
        return out

    def _simulate_single_session(
        self,
        *,
        day_obj: date,
        d_str: str,
        sess: Dict[str, str],
        basket: List[ScreenedCandidate],
        bars_5m_today: Dict[str, List[Bar]],
        bars_1m_today: Dict[str, List[Bar]],
        rolling_5m_history: Dict[str, List[Bar]],
        broker: SimulatedPortfolioBroker,
    ) -> None:
        broker.start_new_day()

        close_t = _parse_hhmm(sess.get("close", "16:00"), (16, 0))
        close_dt_et = datetime.combine(day_obj, close_t, tzinfo=self.tz)

        if self.cfg.enable_early_close_guard:
            entry_cutoff_dt = close_dt_et - timedelta(hours=1)
            flatten_dt = close_dt_et - timedelta(minutes=5)
        else:
            entry_cutoff_dt = datetime.combine(day_obj, time(15, 0), tzinfo=self.tz)
            flatten_dt = datetime.combine(day_obj, time(15, 55), tzinfo=self.tz)

        # Optional custom entry cutoff experiment (e.g. "11:30" or "12:00")
        if self.cfg.custom_entry_cutoff_et:
            custom_cut = datetime.combine(day_obj, _parse_hhmm(self.cfg.custom_entry_cutoff_et, (15, 0)), tzinfo=self.tz)
            if custom_cut < entry_cutoff_dt:
                entry_cutoff_dt = custom_cut

        # Optional midday blackout window experiment (e.g. "12:00"-"14:00")
        blackout_start_dt = (
            datetime.combine(day_obj, _parse_hhmm(self.cfg.midday_blackout_start_et, (12, 0)), tzinfo=self.tz)
            if self.cfg.midday_blackout_start_et
            else None
        )
        blackout_end_dt = (
            datetime.combine(day_obj, _parse_hhmm(self.cfg.midday_blackout_end_et, (14, 0)), tzinfo=self.tz)
            if self.cfg.midday_blackout_end_et
            else None
        )

        flatten_reason = "EOD_FLATTEN"
        if self.cfg.enable_fomc_guard and is_fomc_day_historical(day_obj):
            fomc_cut = datetime.combine(day_obj, _parse_hhmm(self.cfg.fomc_entry_cutoff_time, (13, 30)), tzinfo=self.tz)
            fomc_flat = datetime.combine(day_obj, _parse_hhmm(self.cfg.fomc_flatten_time, (13, 55)), tzinfo=self.tz)
            if fomc_cut < entry_cutoff_dt:
                entry_cutoff_dt = fomc_cut
            if fomc_flat < flatten_dt:
                flatten_dt = fomc_flat
                flatten_reason = "FOMC_FLATTEN"

        or_start_dt = datetime.combine(day_obj, time(9, 30), tzinfo=self.tz)
        or_end_dt = datetime.combine(day_obj, time(9, 45), tzinfo=self.tz)

        or_map: Dict[str, Tuple[float, float]] = {}
        basket_syms = [c.symbol for c in basket]
        cand_by_sym = {c.symbol: c for c in basket}

        for sym in basket_syms:
            sym_5m = bars_5m_today.get(sym, [])
            or_bars = [b for b in sym_5m if or_start_dt <= b.timestamp.astimezone(self.tz) < or_end_dt]
            if or_bars:
                or_map[sym] = (
                    round(max(b.high for b in or_bars), 2),
                    round(min(b.low for b in or_bars), 2),
                )

        all_5m_ts = sorted(
            {
                b.timestamp.astimezone(timezone.utc)
                for sym in (["SPY", "QQQ"] + basket_syms)
                for b in bars_5m_today.get(sym, [])
            }
        )

        map_5m: Dict[str, Dict[datetime, Bar]] = {}
        for sym in ["SPY", "QQQ"] + basket_syms:
            map_5m[sym] = {b.timestamp.astimezone(timezone.utc): b for b in bars_5m_today.get(sym, [])}

        exec_bars_by_ts: Dict[datetime, Dict[str, Bar]] = {}
        for sym in basket_syms:
            src_bars = bars_1m_today.get(sym) if (self.cfg.use_1m_execution and bars_1m_today.get(sym)) else bars_5m_today.get(sym, [])
            for b in src_bars:
                ts_u = b.timestamp.astimezone(timezone.utc)
                exec_bars_by_ts.setdefault(ts_u, {})[sym] = b
        sorted_exec_ts = sorted(exec_bars_by_ts.keys())
        exec_cursor = 0
        last_known_price: Dict[str, float] = {}

        today_5m_accum: Dict[str, List[Bar]] = {s: [] for s in ["SPY", "QQQ"] + basket_syms}
        triggered_today: set[str] = set()
        flattened = False

        def _advance_execution_up_to(target_ts_utc: datetime) -> None:
            nonlocal exec_cursor, flattened
            while exec_cursor < len(sorted_exec_ts) and sorted_exec_ts[exec_cursor] <= target_ts_utc:
                ets = sorted_exec_ts[exec_cursor]
                exec_cursor += 1
                bars_slice = exec_bars_by_ts[ets]
                for s_k, b_v in bars_slice.items():
                    last_known_price[s_k] = b_v.close

                ets_et = ets.astimezone(self.tz)
                if not flattened and ets_et >= flatten_dt:
                    broker.flatten_all(ets, last_known_price, reason=flatten_reason)
                    flattened = True
                    continue

                if not flattened:
                    broker.step_execution_bars(ets, bars_slice)

        for ts_5m_start in all_5m_ts:
            bar_close_utc = ts_5m_start + timedelta(minutes=5)
            bar_close_et = bar_close_utc.astimezone(self.tz)

            _advance_execution_up_to(bar_close_utc - timedelta(seconds=1))

            for sym in ["SPY", "QQQ"] + basket_syms:
                b = map_5m[sym].get(ts_5m_start)
                if b is not None:
                    today_5m_accum[sym].append(b)
                    hist = rolling_5m_history.setdefault(sym, [])
                    hist.append(b)
                    if len(hist) > 60:
                        del hist[:-60]
                    last_known_price[sym] = b.close

            if not flattened and bar_close_et >= flatten_dt:
                broker.flatten_all(bar_close_utc, last_known_price, reason=flatten_reason)
                flattened = True
                continue

            if flattened or bar_close_et <= or_end_dt or bar_close_et >= entry_cutoff_dt:
                continue

            if blackout_start_dt and blackout_end_dt and blackout_start_dt <= bar_close_et < blackout_end_dt:
                continue

            if self.cfg.enable_spy_qqq_gate:
                spy_bars = today_5m_accum["SPY"]
                qqq_bars = today_5m_accum["QQQ"]
                if not spy_bars or not qqq_bars:
                    continue
                spy_vwap = MarketScanner.calculate_intraday_vwap(spy_bars)
                qqq_vwap = MarketScanner.calculate_intraday_vwap(qqq_bars)
                if spy_bars[-1].close < spy_vwap or qqq_bars[-1].close < qqq_vwap:
                    continue

            for sym in basket_syms:
                if sym in triggered_today:
                    continue
                or_pair = or_map.get(sym)
                if or_pair is None:
                    continue

                sym_today = today_5m_accum[sym]
                sym_hist = rolling_5m_history.get(sym, [])
                if len(sym_hist) < 3 or not sym_today:
                    continue
                if ts_5m_start not in map_5m[sym]:
                    continue

                last_bar = sym_today[-1]
                c = last_bar.close
                o = last_bar.open
                or_h, or_l = or_pair

                vwap = MarketScanner.calculate_intraday_vwap(sym_today)
                atr = MarketScanner.calculate_atr(sym_hist, period=14)
                rvol = MarketScanner.calculate_rvol(sym_hist, period=20)

                is_breakout = (
                    (c > or_h)
                    and (c > vwap)
                    and (c > o)
                    and (rvol >= self.cfg.scanner_min_rvol)
                    and ((c - or_h) <= (self.cfg.scanner_max_atr_extension * atr))
                )
                if not is_breakout:
                    continue

                cand = cand_by_sym[sym]
                approved, _ = broker.evaluate_and_submit(
                    symbol=sym,
                    signal_time=bar_close_utc,
                    close_price=c,
                    or_high=or_h,
                    or_low=or_l,
                    atr=atr,
                    signal_rvol=rvol,
                    cm_tech_rating=cand.tech_rating,
                    cm_setup_rating=cand.setup_rating,
                    cm_rs=cand.relative_strength,
                    cm_fundamental_rating=cand.fundamental_rating,
                    cm_sector=cand.sector,
                )

                if approved or self.cfg.lock_triggered_on_reject:
                    triggered_today.add(sym)

        _advance_execution_up_to(close_dt_et.astimezone(timezone.utc))
        if not flattened:
            broker.flatten_all(flatten_dt.astimezone(timezone.utc), last_known_price, reason=flatten_reason)

        flatten_utc = flatten_dt.astimezone(timezone.utc)
        for tr in broker.today_closed_trades:
            sym = tr["symbol"]
            entry_p = float(tr["entry_price"])
            init_stop = float(tr["initial_stop_price"])
            risk_1r = entry_p - init_stop
            if risk_1r <= 0 or not tr.get("exit_time"):
                continue
            exit_dt = datetime.fromisoformat(tr["exit_time"])
            src = bars_1m_today.get(sym) or bars_5m_today.get(sym, [])
            after = [b for b in src if exit_dt < b.timestamp.astimezone(timezone.utc) <= flatten_utc]
            if after:
                post_hi = max(b.high for b in after)
                tr["post_exit_max_r"] = round((post_hi - entry_p) / risk_1r, 3)

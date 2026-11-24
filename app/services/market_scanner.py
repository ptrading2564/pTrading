"""Server-side intraday market scanner executing Opening Range Breakout detection via Alpaca Market Data."""

import asyncio
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo
import logging

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from app.brokers.alpaca_adapter import AlpacaAdapter
from app.config import settings
from app.db import get_connection, get_system_state, record_signal, set_system_state
from app.services.risk_manager import RiskManager
from app.services.telegram_bot import telegram

logger = logging.getLogger(__name__)


class MarketScanner:
    """Monitors the active momentum basket using Alpaca 5m market data.

    Calculates Opening Range (09:30-09:45 ET), VWAP, RVOL, and ATR,
    and dispatches breakout signals directly without requiring TradingView alerts.
    """

    def __init__(self, broker: AlpacaAdapter, risk_manager: RiskManager) -> None:
        self.broker = broker
        self.risk_manager = risk_manager
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)

        feed_choice = DataFeed.IEX if settings.ALPACA_DATA_FEED.lower() == "iex" else DataFeed.SIP
        self.data_feed = feed_choice
        self.data_client = StockHistoricalDataClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
        )

        self.is_running = False
        self.last_scan_time: Optional[datetime] = None
        self.status_message: str = "INITIALIZING"
        self.market_health: Dict[str, Any] = {
            "spy_healthy": False,
            "qqq_healthy": False,
            "spy_price": 0.0,
            "spy_vwap": 0.0,
            "qqq_price": 0.0,
            "qqq_vwap": 0.0,
            "last_checked": None,
        }

        # In-memory OR cache for current date: {symbol: {"or_high": float, "or_low": float, "date": str}}
        self.or_cache: Dict[str, Dict[str, Any]] = {}
        # Daily triggered signals to prevent intra-day re-triggers: {f"{date}_{symbol}"}
        self.triggered_symbols: Set[str] = set()

    async def get_active_basket(self) -> List[str]:
        """Loads the active universe from DB (ChartMill import) or falls back to default basket."""
        basket_str = await get_system_state("latest_chartmill_basket")
        if not basket_str:
            basket_str = settings.DEFAULT_BASKET

        raw_symbols = [s.strip().upper() for s in basket_str.split(",") if s.strip()]
        # Filter duplicates while preserving order
        seen = set()
        unique_symbols = []
        for s in raw_symbols:
            if s not in seen:
                seen.add(s)
                unique_symbols.append(s)
        return unique_symbols

    async def fetch_bars_multi(
        self,
        symbols: List[str],
        start_time: datetime,
        end_time: datetime,
    ) -> Dict[str, List[Any]]:
        """Fetches 5-minute bars for multiple symbols in a non-blocking thread."""
        if not symbols:
            return {}

        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start_time,
            end=end_time,
            feed=self.data_feed,
        )

        try:
            barset = await asyncio.to_thread(self.data_client.get_stock_bars, req)
            result: Dict[str, List[Any]] = {}
            for sym in symbols:
                if sym in barset.data:
                    result[sym] = barset.data[sym]
                else:
                    result[sym] = []
            return result
        except Exception as e:
            logger.error("Error fetching multi-symbol bars from Alpaca: %s", e)
            return {}

    @staticmethod
    def calculate_intraday_vwap(bars: List[Any]) -> float:
        """Calculates cumulative volume-weighted average price for today's bars."""
        if not bars:
            return 0.0

        cum_vol = 0.0
        cum_tp_vol = 0.0
        for b in bars:
            vol = float(b.volume or 0)
            if vol <= 0:
                continue
            typical_price = (float(b.high) + float(b.low) + float(b.close)) / 3.0
            cum_vol += vol
            cum_tp_vol += (typical_price * vol)

        return cum_tp_vol / cum_vol if cum_vol > 0 else float(bars[-1].close)

    @staticmethod
    def calculate_atr(bars: List[Any], period: int = 14) -> float:
        """Calculates ATR(period) over the provided 5m bars."""
        if len(bars) < 2:
            return 1.0

        true_ranges = []
        for i in range(1, len(bars)):
            h = float(bars[i].high)
            l = float(bars[i].low)
            prev_c = float(bars[i - 1].close)
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            true_ranges.append(tr)

        if not true_ranges:
            return 1.0

        window = true_ranges[-period:] if len(true_ranges) >= period else true_ranges
        return sum(window) / len(window)

    @staticmethod
    def calculate_rvol(bars: List[Any], period: int = 20) -> float:
        """Relative Volume: current bar volume divided by 20-period SMA volume."""
        if not bars:
            return 1.0

        cur_vol = float(bars[-1].volume or 0)
        if len(bars) < 2:
            return 1.0

        hist_bars = bars[:-1]
        window = hist_bars[-period:] if len(hist_bars) >= period else hist_bars
        volumes = [float(b.volume or 0) for b in window if float(b.volume or 0) > 0]

        if not volumes:
            return 1.0

        avg_vol = sum(volumes) / len(volumes)
        return cur_vol / avg_vol if avg_vol > 0 else 1.0

    async def evaluate_market_health(self, today_date: str) -> bool:
        """Evaluates SPY & QQQ VWAP status to gate market breakouts."""
        now_et = datetime.now(self.tz)
        session_start_et = datetime.combine(now_et.date(), time(9, 30), tzinfo=self.tz)

        bars_dict = await self.fetch_bars_multi(["SPY", "QQQ"], session_start_et, now_et)

        spy_bars = bars_dict.get("SPY", [])
        qqq_bars = bars_dict.get("QQQ", [])

        if not spy_bars or not qqq_bars:
            self.market_health.update({
                "spy_healthy": False,
                "qqq_healthy": False,
                "last_checked": now_et.strftime("%H:%M:%S"),
            })
            return False

        spy_last_close = float(spy_bars[-1].close)
        spy_vwap = self.calculate_intraday_vwap(spy_bars)
        spy_healthy = spy_last_close >= spy_vwap

        qqq_last_close = float(qqq_bars[-1].close)
        qqq_vwap = self.calculate_intraday_vwap(qqq_bars)
        qqq_healthy = qqq_last_close >= qqq_vwap

        self.market_health.update({
            "spy_healthy": spy_healthy,
            "qqq_healthy": qqq_healthy,
            "spy_price": round(spy_last_close, 2),
            "spy_vwap": round(spy_vwap, 2),
            "qqq_price": round(qqq_last_close, 2),
            "qqq_vwap": round(qqq_vwap, 2),
            "last_checked": now_et.strftime("%H:%M:%S"),
        })

        return spy_healthy and qqq_healthy

    async def update_opening_ranges(self, symbols: List[str], today_date: str) -> None:
        """Builds Opening Range High & Low (09:30-09:45 ET) for all basket symbols."""
        now_et = datetime.now(self.tz)
        or_start_et = datetime.combine(now_et.date(), time(9, 30), tzinfo=self.tz)
        or_end_et = datetime.combine(now_et.date(), time(9, 45), tzinfo=self.tz)

        cutoff = min(now_et, or_end_et)
        bars_dict = await self.fetch_bars_multi(symbols, or_start_et, cutoff)

        for sym, bars in bars_dict.items():
            if not bars:
                continue
            or_high = max(float(b.high) for b in bars)
            or_low = min(float(b.low) for b in bars)
            self.or_cache[sym] = {
                "or_high": round(or_high, 2),
                "or_low": round(or_low, 2),
                "date": today_date,
                "bar_count": len(bars),
            }

    async def scan_once(self) -> List[Dict[str, Any]]:
        """Executes one scan iteration over the basket."""
        now_et = datetime.now(self.tz)
        today_date = now_et.strftime("%Y-%m-%d")
        self.last_scan_time = now_et

        # Clear cache if rolling into a new day
        for sym in list(self.or_cache.keys()):
            if self.or_cache[sym].get("date") != today_date:
                del self.or_cache[sym]

        market_open_et = time(9, 30)
        or_cutoff_et = time(9, 45)
        entry_cutoff_et = time(15, 0)
        market_close_et = time(16, 0)
        cur_t = now_et.time()

        # Check market hours
        if cur_t < market_open_et or cur_t >= market_close_et:
            self.status_message = "MARKET_CLOSED (Awaiting 09:30 ET)"
            return []

        basket = await self.get_active_basket()
        if not basket:
            self.status_message = "EMPTY_BASKET"
            return []

        # 09:30 - 09:45 ET: Building Opening Range
        if cur_t < or_cutoff_et:
            self.status_message = f"BUILDING_OR ({cur_t.strftime('%H:%M:%S')} < 09:45 ET | {len(basket)} symbols)"
            await self.update_opening_ranges(basket, today_date)
            return []

        # 15:00 - 16:00 ET: After entry window cutoff
        if cur_t >= entry_cutoff_et:
            self.status_message = "AFTER_ENTRY_CUTOFF (No new entries after 15:00 ET)"
            return []

        # 09:45 - 15:00 ET: Active Breakout Scanning Window
        # Step 1: Ensure Opening Ranges are established
        symbols_missing_or = [s for s in basket if s not in self.or_cache or self.or_cache[s].get("date") != today_date]
        if symbols_missing_or:
            await self.update_opening_ranges(symbols_missing_or, today_date)

        # Step 2: Check Market Health Gate (SPY & QQQ > VWAP)
        market_healthy = await self.evaluate_market_health(today_date)
        if not market_healthy:
            self.status_message = "GATED_BY_MARKET_HEALTH (SPY or QQQ below VWAP)"
            logger.info("Scanner gated: SPY/QQQ health check failed: %s", self.market_health)
            return []

        self.status_message = f"ACTIVE_SCANNING ({len(basket)} symbols, Market Healthy)"

        # Step 3: Fetch intraday 5m bars for candidate evaluation
        # Start fetch from previous day or today 09:30 to get 20+ bars for RVOL/ATR
        lookback_start = datetime.combine(now_et.date() - timedelta(days=2), time(9, 30), tzinfo=self.tz)
        bars_dict = await self.fetch_bars_multi(basket, lookback_start, now_et)

        triggered_signals: List[Dict[str, Any]] = []

        # Check system global state
        kill_switch = await get_system_state("kill_switch", default="OFF")
        if kill_switch == "ON":
            self.status_message = "KILL_SWITCH_ACTIVE"
            return []

        mode_str = await get_system_state("execution_mode", default=settings.EXECUTION_MODE.value)
        if mode_str == "PAUSED":
            self.status_message = "SYSTEM_PAUSED"
            return []

        for symbol in basket:
            key = f"{today_date}_{symbol}"
            if key in self.triggered_symbols:
                continue

            or_info = self.or_cache.get(symbol)
            if not or_info:
                continue

            bars = bars_dict.get(symbol, [])
            if len(bars) < 3:
                continue

            last_bar = bars[-1]
            c = float(last_bar.close)
            o = float(last_bar.open)
            or_h = float(or_info["or_high"])
            or_l = float(or_info["or_low"])

            # Filter today's bars for VWAP
            today_bars = [b for b in bars if b.timestamp.astimezone(self.tz).date() == now_et.date()]
            vwap = self.calculate_intraday_vwap(today_bars)
            atr = self.calculate_atr(bars, period=14)
            rvol = self.calculate_rvol(bars, period=20)

            # Breakout Conditions:
            # 1. Close > OR High
            # 2. Close > VWAP
            # 3. Bullish candle close (Close > Open)
            # 4. RVOL >= Threshold
            # 5. Extension: (Close - OR High) <= 0.75 * ATR
            is_breakout = (
                (c > or_h)
                and (c > vwap)
                and (c > o)
                and (rvol >= settings.SCANNER_MIN_RVOL)
                and ((c - or_h) <= (settings.SCANNER_MAX_ATR_EXTENSION * atr))
            )

            if not is_breakout:
                continue

            bar_time = int(last_bar.timestamp.timestamp())
            logger.info(
                "🚀 BREAKOUT DETECTED: %s (Close: %.2f > OR_H: %.2f, RVOL: %.2f, ATR: %.2f)",
                symbol, c, or_h, rvol, atr,
            )

            # Record signal in database (idempotency check)
            sig_id = await record_signal(
                bar_time=bar_time,
                symbol=symbol,
                close_price=c,
                or_high=or_h,
                or_low=or_l,
                atr=atr,
                status="RECEIVED",
                raw_payload=f"Server-Side MarketScanner (RVOL: {rvol:.2f})",
            )

            if sig_id is None:
                # Already processed this bar
                continue

            self.triggered_symbols.add(key)

            # Evaluate Risk Manager
            eval_res = await self.risk_manager.evaluate_signal(
                symbol=symbol,
                close_price=c,
                or_high=or_h,
                or_low=or_l,
                atr=atr,
            )

            if not eval_res.approved:
                logger.warning("Signal for %s rejected by RiskManager: %s", symbol, eval_res.rejection_reason)
                async with get_connection() as conn:
                    await conn.execute(
                        "UPDATE signals SET status = 'REJECTED', rejection_reason = ? WHERE id = ?;",
                        (eval_res.rejection_reason, sig_id),
                    )
                    await conn.commit()
                continue

            # Route by execution mode
            if mode_str == "SEMI_AUTO":
                async with get_connection() as conn:
                    await conn.execute("UPDATE signals SET status = 'AWAITING_APPROVAL' WHERE id = ?;", (sig_id,))
                    await conn.commit()

                await telegram.notify_signal(
                    signal_id=sig_id,
                    symbol=symbol,
                    close_price=eval_res.entry_limit_price,
                    qty=eval_res.qty,
                    stop_loss=eval_res.stop_loss_price,
                    take_profit=eval_res.take_profit_price,
                    semi_auto=True,
                )
                triggered_signals.append({"symbol": symbol, "status": "awaiting_approval", "signal_id": sig_id})

            elif mode_str == "AUTO":
                from app.routers.webhook import execute_order_for_signal
                try:
                    atr_ext = round((c - or_h) / atr, 4) if atr > 0 else 0.0
                    or_width_pct = round(((or_h - or_l) / c) * 100.0, 4) if c > 0 else 0.0
                    order_res = await execute_order_for_signal(
                        symbol=symbol,
                        qty=eval_res.qty,
                        entry_limit_price=eval_res.entry_limit_price,
                        stop_loss_price=eval_res.stop_loss_price,
                        take_profit_price=eval_res.take_profit_price,
                        bar_time=bar_time,
                        signal_price=c,
                        stop_type=eval_res.stop_type,
                        signal_rvol=round(rvol, 3),
                        signal_atr_ext=atr_ext,
                        or_width_pct=or_width_pct,
                    )
                    async with get_connection() as conn:
                        await conn.execute("UPDATE signals SET status = 'EXECUTED' WHERE id = ?;", (sig_id,))
                        await conn.commit()
                    triggered_signals.append({"symbol": symbol, "status": "executed", "order": order_res})
                except Exception as e:
                    logger.error("Auto-execution failed for %s: %s", symbol, e)
                    async with get_connection() as conn:
                        await conn.execute(
                            "UPDATE signals SET status = 'FAILED', rejection_reason = ? WHERE id = ?;",
                            (str(e), sig_id),
                        )
                        await conn.commit()

        return triggered_signals

    async def start(self, poll_interval_seconds: Optional[float] = None) -> None:
        """Starts the infinite background scanner loop."""
        interval = poll_interval_seconds or settings.SCANNER_POLL_INTERVAL_SECONDS
        self.is_running = True
        logger.info("MarketScanner background loop started (interval: %.1fs, feed: %s)", interval, self.data_feed.value)

        while self.is_running:
            try:
                await self.scan_once()
            except Exception as e:
                logger.error("Unhandled error in MarketScanner loop: %s", e)
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self.is_running = False
        logger.info("MarketScanner stopped.")

    def get_status_info(self) -> Dict[str, Any]:
        """Provides status dict for Web Dashboard."""
        return {
            "status": self.status_message,
            "last_scan_time": self.last_scan_time.strftime("%H:%M:%S") if self.last_scan_time else None,
            "cached_or_count": len(self.or_cache),
            "triggered_today_count": len(self.triggered_symbols),
            "market_health": self.market_health,
        }


_market_scanner_instance: Optional[MarketScanner] = None


def get_market_scanner() -> MarketScanner:
    """Returns singleton MarketScanner instance."""
    global _market_scanner_instance
    if _market_scanner_instance is None:
        broker = AlpacaAdapter()
        risk_manager = RiskManager(broker)
        _market_scanner_instance = MarketScanner(broker, risk_manager)
    return _market_scanner_instance


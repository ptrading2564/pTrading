"""Lifecycle worker monitoring positions for +1.0R breakeven stepping, stale order cancellation, and 15:55 ET EOD flatten."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo
import logging

from app.brokers.base import BrokerAdapter
from app.config import settings
from app.db import get_connection, get_system_state, save_daily_report, set_system_state
from app.services.analytics_service import analytics_service
from app.services.calendar_service import CalendarService, is_fomc_day
from app.services.financing_service import financing_service
from app.services.strategy_reviewer import strategy_reviewer
from app.services.telegram_bot import telegram

logger = logging.getLogger(__name__)


class LifecycleWorker:
    """Manages the real-time position and order lifecycle."""

    def __init__(self, broker: BrokerAdapter) -> None:
        self.broker = broker
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)
        self.calendar = CalendarService(broker=broker)
        self.is_running = False

    async def step_breakeven_if_earned(self) -> None:
        """Checks open trades. If current price reaches +1.0R, moves Stop-Loss to Entry + $0.02."""
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM trades WHERE is_open = 1 AND breakeven_stepped = 0;"
            )
            open_trades = [dict(r) for r in await cursor.fetchall()]

        if not open_trades:
            return

        try:
            positions = await self.broker.get_positions()
            pos_by_symbol = {p.symbol: p for p in positions}
            open_orders = await self.broker.get_open_orders()
        except Exception as e:
            logger.error("LifecycleWorker error fetching broker state: %s", e)
            return

        for trade in open_trades:
            symbol = trade["symbol"]
            pos = pos_by_symbol.get(symbol)
            if not pos:
                continue

            entry_price = float(trade["entry_price"])
            initial_stop = float(trade.get("initial_stop_price") or trade["stop_loss_price"])
            risk_1r = entry_price - initial_stop

            if risk_1r <= 0:
                continue

            be_target = entry_price + (settings.BREAKEVEN_TRIGGER_R * risk_1r)

            # Trigger condition: market traded at or above +1.0R target
            if pos.current_price >= be_target:
                new_stop = round(entry_price + settings.BREAKEVEN_OFFSET_DOLLARS, 2)
                logger.info(
                    "Symbol %s reached +1.0R (Price: %.2f >= Target: %.2f). Stepping stop to %.2f",
                    symbol,
                    pos.current_price,
                    be_target,
                    new_stop,
                )

                # Locate the active stop-loss order leg for this symbol
                stop_order_id = None
                for order in open_orders:
                    if order.symbol == symbol:
                        # Check top-level order or child legs
                        if order.stop_price is not None and order.status in ("new", "accepted", "held", "pending_replace"):
                            stop_order_id = order.order_id
                            break
                        if order.legs:
                            for leg in order.legs:
                                if leg.stop_price is not None and leg.status in ("new", "accepted", "held", "pending_replace"):
                                    stop_order_id = leg.order_id
                                    break
                    if stop_order_id:
                        break

                if stop_order_id:
                    success = await self.broker.replace_stop_loss(stop_order_id, new_stop)
                    if success:
                        async with get_connection() as conn:
                            await conn.execute(
                                """
                                UPDATE trades
                                SET breakeven_stepped = 1,
                                    initial_stop_price = COALESCE(initial_stop_price, stop_loss_price),
                                    stop_loss_price = ?
                                WHERE id = ?;
                                """,
                                (new_stop, trade["id"]),
                            )
                            await conn.commit()

                        await telegram.notify_breakeven_stepped(symbol, pos.current_price, new_stop)
                else:
                    logger.warning("Could not find active stop-loss order leg for %s to replace", symbol)

    async def cancel_stale_orders(self) -> None:
        """Cancels unfilled entry limit orders older than 3 minutes (180 seconds)."""
        try:
            open_orders = await self.broker.get_open_orders()
        except Exception as e:
            logger.error("Error fetching open orders in cancel_stale_orders: %s", e)
            return

        now_utc = datetime.now(timezone.utc)
        timeout_delta = timedelta(seconds=settings.STALE_ORDER_TIMEOUT_SECONDS)

        for order in open_orders:
            # We only cancel pending BUY entry orders, not bracket exit legs
            if order.side.lower() == "buy" and order.status in ("new", "accepted", "pending_new"):
                created_at = order.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)

                age = now_utc - created_at
                if age >= timeout_delta:
                    logger.warning(
                        "Cancelling stale unfilled entry order %s for %s (age: %d seconds)",
                        order.order_id,
                        order.symbol,
                        int(age.total_seconds()),
                    )
                    cancelled = await self.broker.cancel_order(order.order_id)
                    if cancelled:
                        async with get_connection() as conn:
                            await conn.execute(
                                "UPDATE orders SET status = 'CANCELLED' WHERE broker_order_id = ? OR client_order_id = ?;",
                                (order.order_id, order.client_order_id),
                            )
                            await conn.commit()
                        await telegram.send_message(
                            f"⏱️ <b>Stale Order Expired:</b> Unfilled entry order for <code>{order.symbol}</code> cancelled after 3 minutes."
                        )

    async def check_eod_flatten(self) -> None:
        """Executes EOD flatten at 15:55 ET (or 12:55 ET on early close days)."""
        now_et = datetime.now(self.tz)
        today_str = now_et.strftime("%Y-%m-%d")

        last_flatten_done = await get_system_state("eod_flatten_done")
        if last_flatten_done == today_str:
            return

        try:
            market_close = await self.broker.get_market_close_time()
        except Exception as e:
            logger.error("Failed to query market close time: %s", e)
            return

        # Flatten cutoff is exactly 5 minutes before official market close
        flatten_time = market_close - timedelta(minutes=5)

        if now_et >= flatten_time:
            try:
                if not await self.broker.is_market_open():
                    logger.debug("Skipping EOD flatten check: regular market session is already closed.")
                    return
            except Exception as e:
                logger.warning("Could not confirm market open status before EOD flatten: %s", e)
                return

            logger.warning(
                "EOD Flatten Triggered! Current ET: %s >= Cutoff: %s",
                now_et.strftime("%H:%M:%S"),
                flatten_time.strftime("%H:%M:%S"),
            )

            positions = await self.broker.get_positions()
            pos_count = len(positions)
            pos_prices = {p.symbol: float(p.current_price) for p in positions}

            await self.broker.close_all_positions()
            await asyncio.sleep(2.0)

            # Verify Alpaca actually flattened all positions; retry once if bracket legs delayed release
            remaining = await self.broker.get_positions()
            if remaining:
                logger.warning(
                    "EOD Flatten retry: %d position(s) still open (%s). Retrying close_all_positions...",
                    len(remaining),
                    ", ".join(p.symbol for p in remaining),
                )
                await self.broker.close_all_positions()
                await asyncio.sleep(2.0)
                remaining = await self.broker.get_positions()

            remaining_symbols = {p.symbol for p in remaining}

            # Reconcile and close ONLY DB trades whose symbols are confirmed flat at Alpaca
            closed_orders = await self.broker.get_closed_orders(
                after=datetime.now(timezone.utc) - timedelta(hours=12)
            )
            async with get_connection() as conn:
                cursor = await conn.execute("SELECT * FROM trades WHERE is_open = 1;")
                open_trades = [dict(r) for r in await cursor.fetchall()]
                for trade in open_trades:
                    if trade["symbol"] in remaining_symbols:
                        continue
                    await self._finalize_closed_trade(
                        conn=conn,
                        trade=trade,
                        closed_orders=closed_orders,
                        default_reason="EOD_FLATTEN",
                        fallback_exit_price=pos_prices.get(trade["symbol"]),
                    )
                await conn.commit()

            await self.update_daily_stats_snapshot()
            if not remaining:
                await set_system_state("eod_flatten_done", today_str)
                await telegram.notify_eod_flatten(pos_count)
            else:
                rem_list = ", ".join(sorted(remaining_symbols))
                logger.error("EOD Flatten incomplete! Still open at broker: %s", rem_list)
                await financing_service.latch_dead_mans_brake(
                    f"EOD Flatten incomplete: {len(remaining)} position(s) still open ({rem_list})."
                )
                await telegram.send_message(
                    f"⚠️ <b>EOD Flatten Warning (Dead Man's Brake Latched):</b> {len(remaining)} position(s) still open at Alpaca: <code>{rem_list}</code>. Reconcile & reset on /financing."
                )

    async def _finalize_closed_trade(
        self,
        conn,
        trade: dict,
        closed_orders: list,
        default_reason: str = "BRACKET_EXIT",
        fallback_exit_price: Optional[float] = None,
    ) -> None:
        """Reconciles actual entry/exit fills, slippage, P&L ($ and R), and exit reason for a closed trade."""
        symbol = trade["symbol"]
        qty = int(trade["qty"])
        client_order_id = trade["client_order_id"]
        entry_price = float(trade["entry_price"])
        initial_stop = float(trade.get("initial_stop_price") or trade["stop_loss_price"])
        take_profit = float(trade["take_profit_price"])
        breakeven_stepped = bool(trade.get("breakeven_stepped", 0))
        signal_price = float(trade.get("signal_price") or entry_price)

        # Look up parent order for actual entry fill price and bracket exit legs
        parent_order = next(
            (o for o in closed_orders if o.client_order_id == client_order_id), None
        )
        if parent_order is None:
            parent_order = await self.broker.get_order_by_client_id(client_order_id)

        if parent_order and parent_order.filled_avg_price:
            entry_price = float(parent_order.filled_avg_price)

        exit_price: Optional[float] = None
        exit_time_str: Optional[str] = None
        exit_reason = default_reason

        # Check nested bracket legs first
        if parent_order and parent_order.legs:
            for leg in parent_order.legs:
                leg_status = str(leg.status).lower()
                if "filled" in leg_status and leg.filled_avg_price:
                    exit_price = float(leg.filled_avg_price)
                    if leg.filled_at:
                        exit_time_str = leg.filled_at.isoformat()
                    if leg.stop_price is not None:
                        exit_reason = "BREAKEVEN" if breakeven_stepped else "STOP_LOSS"
                    elif leg.limit_price is not None:
                        exit_reason = "TAKE_PROFIT"
                    break

        # Fallback: check standalone closed SELL orders for this symbol
        if exit_price is None:
            sell_matches = [
                o
                for o in closed_orders
                if o.symbol == symbol
                and "sell" in str(o.side).lower()
                and "filled" in str(o.status).lower()
                and o.filled_avg_price
            ]
            if sell_matches:
                latest_sell = max(
                    sell_matches,
                    key=lambda o: o.filled_at or o.created_at,
                )
                exit_price = float(latest_sell.filled_avg_price)
                if latest_sell.filled_at:
                    exit_time_str = latest_sell.filled_at.isoformat()

        # Final fallback (e.g. market price captured right before flatten)
        if exit_price is None and fallback_exit_price is not None:
            exit_price = float(fallback_exit_price)

        # Infer reason if still generic BRACKET_EXIT and we have an exit price
        if exit_reason == "BRACKET_EXIT" and exit_price is not None:
            if exit_price >= take_profit - 0.05:
                exit_reason = "TAKE_PROFIT"
            elif breakeven_stepped and abs(exit_price - entry_price) <= max(0.10, abs(entry_price - initial_stop) * 0.15):
                exit_reason = "BREAKEVEN"
            elif exit_price <= initial_stop + 0.05:
                exit_reason = "STOP_LOSS"

        risk_per_share = max(entry_price - initial_stop, 0.0)
        initial_risk_dollars = float(trade.get("initial_risk_dollars") or round(risk_per_share * qty, 2))
        if initial_risk_dollars <= 0 and risk_per_share > 0:
            initial_risk_dollars = round(risk_per_share * qty, 2)

        slippage_dollars = round(entry_price - signal_price, 4)
        slippage_r = round(slippage_dollars / risk_per_share, 4) if risk_per_share > 0 else 0.0

        pnl_dollars: Optional[float] = None
        pnl_r: Optional[float] = None
        if exit_price is not None:
            pnl_dollars = round((exit_price - entry_price) * qty, 2)
            pnl_r = round(pnl_dollars / initial_risk_dollars, 4) if initial_risk_dollars > 0 else 0.0

        entry_date_fallback = str(trade.get("entry_time") or "")[:10]
        trade_date = (
            trade.get("trade_date")
            or (entry_date_fallback if len(entry_date_fallback) == 10 else None)
            or datetime.now(self.tz).strftime("%Y-%m-%d")
        )

        await conn.execute(
            """
            UPDATE trades
            SET is_open = 0,
                entry_price = ?,
                initial_stop_price = COALESCE(initial_stop_price, ?),
                initial_risk_dollars = ?,
                slippage_dollars = ?,
                slippage_r = ?,
                exit_price = ?,
                exit_time = COALESCE(?, CURRENT_TIMESTAMP),
                pnl_dollars = ?,
                pnl_r = ?,
                exit_reason = ?,
                trade_date = ?
            WHERE id = ?;
            """,
            (
                entry_price,
                initial_stop,
                initial_risk_dollars,
                slippage_dollars,
                slippage_r,
                exit_price,
                exit_time_str,
                pnl_dollars,
                pnl_r,
                exit_reason,
                trade_date,
                trade["id"],
            ),
        )

    async def update_daily_stats_snapshot(self) -> None:
        """Updates today's daily_stats row with live equity, realized P&L, unrealized P&L, and trade count."""
        today_str = datetime.now(self.tz).strftime("%Y-%m-%d")
        try:
            equity = await self.broker.get_account_equity()
            positions = await self.broker.get_positions()
            unrealized_pnl = round(sum(float(p.unrealized_pnl) for p in positions), 2)
        except Exception as e:
            logger.debug("Skipping daily_stats snapshot (broker unavailable): %s", e)
            return

        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT COUNT(*) as cnt, COALESCE(SUM(pnl_dollars), 0.0) as realized
                FROM trades
                WHERE is_open = 0 AND COALESCE(trade_date, substr(entry_time, 1, 10)) = ?;
                """,
                (today_str,),
            )
            agg = await cursor.fetchone()
            trade_count = int(agg["cnt"]) if agg else 0
            realized_pnl = round(float(agg["realized"]), 2) if agg else 0.0

            existing = await (
                await conn.execute("SELECT date FROM daily_stats WHERE date = ?;", (today_str,))
            ).fetchone()
            if existing:
                await conn.execute(
                    """
                    UPDATE daily_stats
                    SET current_equity = ?, realized_pnl = ?, unrealized_pnl = ?,
                        trade_count = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE date = ?;
                    """,
                    (equity, realized_pnl, unrealized_pnl, trade_count, today_str),
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO daily_stats (date, starting_equity, current_equity, realized_pnl, unrealized_pnl, trade_count)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (today_str, equity, equity, realized_pnl, unrealized_pnl, trade_count),
                )
            await conn.commit()

    async def sync_closed_trades(self) -> None:
        """Synchronizes DB open trades against live broker positions to record TP/SL exits with exact P&L."""
        try:
            positions = await self.broker.get_positions()
            active_symbols = {p.symbol for p in positions}
        except Exception:
            return

        async with get_connection() as conn:
            cursor = await conn.execute("SELECT * FROM trades WHERE is_open = 1;")
            open_trades = [dict(r) for r in await cursor.fetchall()]

            closed_candidates = [t for t in open_trades if t["symbol"] not in active_symbols]
            if not closed_candidates:
                return

            closed_orders = await self.broker.get_closed_orders(
                after=datetime.now(timezone.utc) - timedelta(hours=12)
            )
            for trade in closed_candidates:
                await self._finalize_closed_trade(
                    conn=conn,
                    trade=trade,
                    closed_orders=closed_orders,
                    default_reason="BRACKET_EXIT",
                )
            await conn.commit()

        await self.update_daily_stats_snapshot()

    async def backfill_missing_trade_exits(self, lookback_days: int = 14) -> int:
        """Re-opens trades still active at Alpaca and backfills missing exit fills for actually-closed trades."""
        try:
            live_positions = await self.broker.get_positions()
            active_symbols = {p.symbol for p in live_positions}
        except Exception:
            active_symbols = set()

        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM trades WHERE is_open = 0 AND (exit_price IS NULL OR pnl_dollars IS NULL);"
            )
            missing_trades = [dict(r) for r in await cursor.fetchall()]

        if not missing_trades:
            return 0

        closed_orders = await self.broker.get_closed_orders(
            after=datetime.now(timezone.utc) - timedelta(days=lookback_days)
        )
        updated = 0
        reopened = 0
        async with get_connection() as conn:
            for trade in missing_trades:
                # If broker still holds an active position for this symbol, re-open in DB instead of faking a $0 exit
                if trade["symbol"] in active_symbols:
                    await conn.execute(
                        """
                        UPDATE trades
                        SET is_open = 1, exit_reason = NULL, exit_time = NULL,
                            exit_price = NULL, pnl_dollars = NULL, pnl_r = NULL
                        WHERE id = ?;
                        """,
                        (trade["id"],),
                    )
                    reopened += 1
                    continue

                await self._finalize_closed_trade(
                    conn=conn,
                    trade=trade,
                    closed_orders=closed_orders,
                    default_reason=str(trade.get("exit_reason") or "BRACKET_EXIT"),
                )
                updated += 1
            await conn.commit()

        # Re-sync daily_stats.realized_pnl and trade_count using only reconciled closed trades
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE daily_stats
                SET realized_pnl = COALESCE((
                        SELECT ROUND(SUM(pnl_dollars), 2)
                        FROM trades
                        WHERE is_open = 0 AND exit_price IS NOT NULL AND pnl_dollars IS NOT NULL
                          AND COALESCE(trade_date, substr(entry_time, 1, 10)) = daily_stats.date
                    ), 0.0),
                    trade_count = COALESCE((
                        SELECT COUNT(*)
                        FROM trades
                        WHERE is_open = 0 AND exit_price IS NOT NULL AND pnl_dollars IS NOT NULL
                          AND COALESCE(trade_date, substr(entry_time, 1, 10)) = daily_stats.date
                    ), 0);
                """
            )
            await conn.commit()

        logger.info(
            "Backfill complete: reconciled %d closed trade(s), re-opened %d still-active position(s).",
            updated,
            reopened,
        )
        return updated

    async def generate_and_persist_daily_report(
        self, report_date: Optional[str] = None, enrich_bars: bool = True
    ) -> dict:
        """Runs full EOD backfill, enrichment, analytics, and strategy review and persists to daily_reports."""
        target_date = report_date or datetime.now(self.tz).strftime("%Y-%m-%d")
        await self.backfill_missing_trade_exits()
        await self.update_daily_stats_snapshot()
        if enrich_bars:
            await analytics_service.enrich_eod_excursions_and_regime(target_date)

        report = await analytics_service.build_comprehensive_report(report_date=target_date)
        review_md, review_source, diagnostics = await strategy_reviewer.generate_review(report)
        report["diagnostics"] = diagnostics

        await save_daily_report(
            report_date=target_date,
            metrics=report,
            review_markdown=review_md,
            review_source=review_source,
        )
        return {
            "report": report,
            "review_markdown": review_md,
            "review_source": review_source,
            "diagnostics": diagnostics,
        }

    async def check_eod_daily_report(self) -> None:
        """Generates and sends the automated EOD comprehensive analysis after market close."""
        now_et = datetime.now(self.tz)
        today_str = now_et.strftime("%Y-%m-%d")

        last_report_sent = await get_system_state("daily_report_sent")
        if last_report_sent == today_str:
            return

        # Skip automated EOD report on weekends and NYSE market holidays (e.g. Thanksgiving)
        if not await self.calendar.is_trading_day(now_et.date()):
            return

        try:
            market_close = await self.broker.get_market_close_time()
        except Exception as e:
            logger.debug("Skipping EOD report schedule check (calendar unavailable): %s", e)
            return

        report_time = market_close + timedelta(minutes=settings.DAILY_REPORT_DELAY_MINUTES)
        if now_et >= report_time:
            logger.info("Generating automated EOD Comprehensive Trading Analysis for %s...", today_str)
            try:
                await self.sync_closed_trades()
                result = await self.generate_and_persist_daily_report(today_str, enrich_bars=True)
                await set_system_state("daily_report_sent", today_str)
                await telegram.notify_daily_report(result["report"], result["diagnostics"])
                logger.info("EOD Daily Comprehensive Report saved and sent for %s.", today_str)
            except Exception as e:
                logger.error("Failed to generate/send EOD daily report for %s: %s", today_str, e)

    async def check_fomc_pre_event_flatten(self) -> None:
        """Flattens all open positions ahead of the 14:00 ET FOMC rate decision to avoid whipsaw slippage."""
        if not settings.FOMC_GUARD_ENABLED:
            return

        now_et = datetime.now(self.tz)
        if not is_fomc_day(now_et.date()):
            return

        today_str = now_et.strftime("%Y-%m-%d")
        if await get_system_state("fomc_flatten_done") == today_str:
            return

        try:
            fh, fm = map(int, settings.FOMC_PRE_EVENT_FLATTEN_TIME.split(":"))
            flatten_cutoff = now_et.replace(hour=fh, minute=fm, second=0, microsecond=0)
        except Exception:
            flatten_cutoff = now_et.replace(hour=13, minute=55, second=0, microsecond=0)

        if now_et < flatten_cutoff:
            return

        try:
            if not await self.broker.is_market_open():
                return
        except Exception:
            return

        logger.warning(
            "FOMC Guard Pre-Event Flatten triggered at %s ET (>= %s ET).",
            now_et.strftime("%H:%M:%S"),
            settings.FOMC_PRE_EVENT_FLATTEN_TIME,
        )

        positions = await self.broker.get_positions()
        pos_count = len(positions)
        pos_prices = {p.symbol: float(p.current_price) for p in positions}

        if pos_count > 0:
            await self.broker.close_all_positions()
            await asyncio.sleep(2.0)
            remaining = await self.broker.get_positions()
            if remaining:
                await self.broker.close_all_positions()
                await asyncio.sleep(2.0)
                remaining = await self.broker.get_positions()
        else:
            remaining = []

        remaining_symbols = {p.symbol for p in remaining}
        closed_orders = await self.broker.get_closed_orders(
            after=datetime.now(timezone.utc) - timedelta(hours=8)
        )
        async with get_connection() as conn:
            cursor = await conn.execute("SELECT * FROM trades WHERE is_open = 1;")
            open_trades = [dict(r) for r in await cursor.fetchall()]
            for trade in open_trades:
                if trade["symbol"] in remaining_symbols:
                    continue
                await self._finalize_closed_trade(
                    conn=conn,
                    trade=trade,
                    closed_orders=closed_orders,
                    default_reason="FOMC_FLATTEN",
                    fallback_exit_price=pos_prices.get(trade["symbol"]),
                )
            await conn.commit()

        await self.update_daily_stats_snapshot()
        if not remaining:
            await set_system_state("fomc_flatten_done", today_str)
            await telegram.send_message(
                f"🛡️ <b>FOMC Guard Pre-Event Flatten ({settings.FOMC_PRE_EVENT_FLATTEN_TIME} ET)</b>\n"
                f"Closed {pos_count} open position(s) ahead of the 14:00 ET FOMC rate decision. "
                f"Entries remain halted for the rest of today's session."
            )
        else:
            rem_list = ", ".join(sorted(remaining_symbols))
            logger.error("FOMC Flatten incomplete! Still open at broker: %s", rem_list)
            await telegram.send_message(
                f"⚠️ <b>FOMC Flatten Warning:</b> {len(remaining)} position(s) still open: <code>{rem_list}</code>. Will retry."
            )

    async def run_iteration(self) -> None:
        """Single tick of the lifecycle worker."""
        await self.step_breakeven_if_earned()
        await self.cancel_stale_orders()
        await self.check_fomc_pre_event_flatten()
        await self.check_eod_flatten()
        await self.sync_closed_trades()
        await self.check_eod_daily_report()

    async def start(self, poll_interval_seconds: float = 5.0) -> None:
        """Starts the infinite background lifecycle monitor loop."""
        self.is_running = True
        consecutive_errors = 0
        logger.info("LifecycleWorker started with %.1fs polling interval", poll_interval_seconds)
        while self.is_running:
            try:
                await self.run_iteration()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                logger.error("Unhandled error in LifecycleWorker loop (%d consecutive): %s", consecutive_errors, e)
                if consecutive_errors >= 5:
                    await financing_service.latch_dead_mans_brake(
                        f"LifecycleWorker hit {consecutive_errors} consecutive loop failures: {e}"
                    )
            await asyncio.sleep(poll_interval_seconds)

    def stop(self) -> None:
        self.is_running = False
        logger.info("LifecycleWorker stopped.")

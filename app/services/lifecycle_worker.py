"""Lifecycle worker monitoring positions for +1.0R breakeven stepping, stale order cancellation, and 15:55 ET EOD flatten."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo
import logging

from app.brokers.base import BrokerAdapter
from app.config import settings
from app.db import get_connection, get_system_state, set_system_state
from app.services.telegram_bot import telegram

logger = logging.getLogger(__name__)


class LifecycleWorker:
    """Manages the real-time position and order lifecycle."""

    def __init__(self, broker: BrokerAdapter) -> None:
        self.broker = broker
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)
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
            initial_stop = float(trade["stop_loss_price"])
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
                                SET breakeven_stepped = 1, stop_loss_price = ?
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
            logger.warning(
                "EOD Flatten Triggered! Current ET: %s >= Cutoff: %s",
                now_et.strftime("%H:%M:%S"),
                flatten_time.strftime("%H:%M:%S"),
            )

            positions = await self.broker.get_positions()
            pos_count = len(positions)

            await self.broker.close_all_positions()

            # Mark all open trades in DB as closed by EOD_FLATTEN
            async with get_connection() as conn:
                await conn.execute(
                    """
                    UPDATE trades
                    SET is_open = 0, exit_time = CURRENT_TIMESTAMP, exit_reason = 'EOD_FLATTEN'
                    WHERE is_open = 1;
                    """
                )
                await conn.commit()

            await set_system_state("eod_flatten_done", today_str)
            await telegram.notify_eod_flatten(pos_count)

    async def sync_closed_trades(self) -> None:
        """Synchronizes DB open trades against live broker positions to record TP/SL exits."""
        try:
            positions = await self.broker.get_positions()
            active_symbols = {p.symbol for p in positions}
        except Exception:
            return

        async with get_connection() as conn:
            cursor = await conn.execute("SELECT * FROM trades WHERE is_open = 1;")
            open_trades = [dict(r) for r in await cursor.fetchall()]

            for trade in open_trades:
                symbol = trade["symbol"]
                # If symbol is no longer in live positions, it has been closed by Alpaca bracket order
                if symbol not in active_symbols:
                    await conn.execute(
                        """
                        UPDATE trades
                        SET is_open = 0, exit_time = CURRENT_TIMESTAMP, exit_reason = 'BRACKET_EXIT'
                        WHERE id = ?;
                        """,
                        (trade["id"],),
                    )
            await conn.commit()

    async def run_iteration(self) -> None:
        """Single tick of the lifecycle worker."""
        await self.step_breakeven_if_earned()
        await self.cancel_stale_orders()
        await self.check_eod_flatten()
        await self.sync_closed_trades()

    async def start(self, poll_interval_seconds: float = 5.0) -> None:
        """Starts the infinite background lifecycle monitor loop."""
        self.is_running = True
        logger.info("LifecycleWorker started with %.1fs polling interval", poll_interval_seconds)
        while self.is_running:
            try:
                await self.run_iteration()
            except Exception as e:
                logger.error("Unhandled error in LifecycleWorker loop: %s", e)
            await asyncio.sleep(poll_interval_seconds)

    def stop(self) -> None:
        self.is_running = False
        logger.info("LifecycleWorker stopped.")

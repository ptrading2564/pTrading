"""Risk management service implementing dynamic 0.5% compounding sizing, 4-position cap, and -2.0% daily circuit breaker."""

from dataclasses import dataclass
from datetime import datetime
from math import floor
from typing import Optional, Tuple
from zoneinfo import ZoneInfo
import logging

from app.brokers.base import BrokerAdapter
from app.config import settings
from app.db import get_connection

logger = logging.getLogger(__name__)


@dataclass
class RiskEvaluation:
    approved: bool
    rejection_reason: Optional[str]
    qty: int
    entry_limit_price: float
    stop_loss_price: float
    take_profit_price: float
    risk_dollars: float
    stop_distance: float
    stop_type: str = "ATR_1_5"


class RiskManager:
    """Evaluates risk and determines position sizing for trade entry signals."""

    def __init__(self, broker: BrokerAdapter) -> None:
        self.broker = broker
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)

    async def get_or_initialize_daily_stats(self, current_equity: float) -> Tuple[float, bool]:
        """Returns (starting_equity, circuit_breaker_tripped) for today."""
        today_str = datetime.now(self.tz).strftime("%Y-%m-%d")

        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT starting_equity, circuit_breaker_tripped FROM daily_stats WHERE date = ?;",
                (today_str,),
            )
            row = await cursor.fetchone()

            if row:
                return float(row["starting_equity"]), bool(row["circuit_breaker_tripped"])

            # First query of the day, initialize daily baseline
            await conn.execute(
                """
                INSERT INTO daily_stats (date, starting_equity, current_equity, circuit_breaker_tripped)
                VALUES (?, ?, ?, 0);
                """,
                (today_str, current_equity, current_equity),
            )
            await conn.commit()
            return current_equity, False

    async def update_daily_stats(self, current_equity: float, breaker_tripped: bool = False) -> None:
        today_str = datetime.now(self.tz).strftime("%Y-%m-%d")
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE daily_stats
                SET current_equity = ?, circuit_breaker_tripped = ?, updated_at = CURRENT_TIMESTAMP
                WHERE date = ?;
                """,
                (current_equity, 1 if breaker_tripped else 0, today_str),
            )
            await conn.commit()

    async def evaluate_signal(
        self,
        symbol: str,
        close_price: float,
        or_high: float,
        or_low: float,
        atr: float,
    ) -> RiskEvaluation:
        """Evaluates an incoming momentum breakout signal against compounding risk and portfolio caps."""

        # 1. Fetch live account equity
        try:
            equity = await self.broker.get_account_equity()
        except Exception as e:
            logger.error("Failed to fetch account equity: %s", e)
            return RiskEvaluation(
                approved=False,
                rejection_reason=f"Broker equity check failed: {e}",
                qty=0,
                entry_limit_price=close_price,
                stop_loss_price=0.0,
                take_profit_price=0.0,
                risk_dollars=0.0,
                stop_distance=0.0,
            )

        # 2. Check Daily Max Loss Circuit Breaker (-2.0%)
        starting_equity, breaker_tripped = await self.get_or_initialize_daily_stats(equity)
        if breaker_tripped:
            return RiskEvaluation(
                approved=False,
                rejection_reason="Daily -2.0% circuit breaker is ACTIVE. Trading halted for today.",
                qty=0,
                entry_limit_price=close_price,
                stop_loss_price=0.0,
                take_profit_price=0.0,
                risk_dollars=0.0,
                stop_distance=0.0,
            )

        daily_drawdown_pct = ((equity - starting_equity) / starting_equity) * 100.0
        if daily_drawdown_pct <= -settings.DAILY_MAX_LOSS_PERCENT:
            logger.critical(
                "Daily circuit breaker tripped! Drawdown is %.2f%% (threshold: -%.2f%%)",
                daily_drawdown_pct,
                settings.DAILY_MAX_LOSS_PERCENT,
            )
            await self.update_daily_stats(equity, breaker_tripped=True)
            return RiskEvaluation(
                approved=False,
                rejection_reason=f"Daily circuit breaker tripped: {daily_drawdown_pct:.2f}% drawdown.",
                qty=0,
                entry_limit_price=close_price,
                stop_loss_price=0.0,
                take_profit_price=0.0,
                risk_dollars=0.0,
                stop_distance=0.0,
            )

        # 3. Check Max Concurrent Positions Cap (Max 4)
        positions = await self.broker.get_positions()
        # Also check if we already hold or have pending orders for this symbol
        already_holding = any(p.symbol == symbol for p in positions)
        if already_holding:
            return RiskEvaluation(
                approved=False,
                rejection_reason=f"Already holding an active position in {symbol}.",
                qty=0,
                entry_limit_price=close_price,
                stop_loss_price=0.0,
                take_profit_price=0.0,
                risk_dollars=0.0,
                stop_distance=0.0,
            )

        open_orders = await self.broker.get_open_orders()
        pending_symbols = {o.symbol for o in open_orders}
        if symbol in pending_symbols:
            return RiskEvaluation(
                approved=False,
                rejection_reason=f"Pending entry order already exists for {symbol}.",
                qty=0,
                entry_limit_price=close_price,
                stop_loss_price=0.0,
                take_profit_price=0.0,
                risk_dollars=0.0,
                stop_distance=0.0,
            )

        total_active = len(positions) + len([o for o in open_orders if o.side == "buy"])
        if total_active >= settings.MAX_CONCURRENT_POSITIONS:
            return RiskEvaluation(
                approved=False,
                rejection_reason=f"Max concurrent positions cap reached ({total_active}/{settings.MAX_CONCURRENT_POSITIONS}).",
                qty=0,
                entry_limit_price=close_price,
                stop_loss_price=0.0,
                take_profit_price=0.0,
                risk_dollars=0.0,
                stop_distance=0.0,
            )

        # 4. Entry & Stop Sizing Calculation
        # Entry price: close_price (marketable limit with 0.05% slippage allowance)
        entry_limit_price = round(close_price * 1.0005, 2)

        # Stop-Loss: max(OR_Low, Entry - 1.5 * ATR)
        atr_stop = round(close_price - 1.5 * atr, 2)
        stop_loss_price = round(max(or_low, atr_stop), 2)
        stop_type = "OR_LOW" if or_low >= atr_stop else "ATR_1_5"

        # Prevent stop above entry or too tight
        stop_distance = round(entry_limit_price - stop_loss_price, 2)
        if stop_distance < settings.MIN_STOP_DISTANCE_DOLLARS:
            return RiskEvaluation(
                approved=False,
                rejection_reason=f"Stop distance too narrow (${stop_distance:.2f} < ${settings.MIN_STOP_DISTANCE_DOLLARS:.2f}).",
                qty=0,
                entry_limit_price=entry_limit_price,
                stop_loss_price=stop_loss_price,
                take_profit_price=0.0,
                risk_dollars=0.0,
                stop_distance=stop_distance,
            )

        # 5. Position Sizing: 0.5% live equity risked per trade
        risk_dollars = equity * (settings.RISK_PERCENT_PER_TRADE / 100.0)
        raw_shares = floor(risk_dollars / stop_distance)

        # 6. Max 25% single stock allocation cap
        max_capital_for_stock = equity * (settings.MAX_EQUITY_PERCENT_PER_STOCK / 100.0)
        max_allowed_shares = floor(max_capital_for_stock / entry_limit_price)

        shares = min(raw_shares, max_allowed_shares)
        if shares <= 0:
            return RiskEvaluation(
                approved=False,
                rejection_reason="Position sizing resulted in 0 shares under capital constraints.",
                qty=0,
                entry_limit_price=entry_limit_price,
                stop_loss_price=stop_loss_price,
                take_profit_price=0.0,
                risk_dollars=risk_dollars,
                stop_distance=stop_distance,
            )

        # 7. Take-Profit: +2.0R
        take_profit_price = round(entry_limit_price + 2.0 * stop_distance, 2)

        return RiskEvaluation(
            approved=True,
            rejection_reason=None,
            qty=shares,
            entry_limit_price=entry_limit_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            risk_dollars=risk_dollars,
            stop_distance=stop_distance,
            stop_type=stop_type,
        )

"""Alpaca Broker Adapter implementation using alpaca-py with non-blocking async execution."""

import asyncio
from datetime import datetime, time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
import logging

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from app.brokers.base import BrokerAdapter, OrderInfo, PositionInfo
from app.config import settings

logger = logging.getLogger(__name__)


class AlpacaAdapter(BrokerAdapter):
    """Production Alpaca implementation supporting bracket orders, stop stepping, and clock/calendar queries."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        paper: Optional[bool] = None,
    ) -> None:
        self.api_key = api_key or settings.ALPACA_API_KEY
        self.secret_key = secret_key or settings.ALPACA_SECRET_KEY
        self.paper = paper if paper is not None else settings.ALPACA_PAPER
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)

        # Initialize alpaca-py TradingClient
        self.client = TradingClient(
            api_key=self.api_key,
            secret_key=self.secret_key,
            paper=self.paper,
        )

    async def get_account_equity(self) -> float:
        """Fetch live account equity."""
        account = await asyncio.to_thread(self.client.get_account)
        return float(account.equity)

    async def get_positions(self) -> List[PositionInfo]:
        """Fetch all open positions."""
        raw_positions = await asyncio.to_thread(self.client.get_all_positions)
        positions: List[PositionInfo] = []
        for p in raw_positions:
            positions.append(
                PositionInfo(
                    symbol=p.symbol,
                    qty=int(p.qty),
                    entry_price=float(p.avg_entry_price),
                    current_price=float(p.current_price),
                    unrealized_pnl=float(p.unrealized_pl),
                    market_value=float(p.market_value),
                )
            )
        return positions

    @staticmethod
    def _parse_order(o: Any) -> OrderInfo:
        legs: List[OrderInfo] = []
        if getattr(o, "legs", None):
            for leg in o.legs:
                legs.append(
                    OrderInfo(
                        order_id=str(leg.id),
                        client_order_id=str(leg.client_order_id or ""),
                        symbol=leg.symbol,
                        status=str(leg.status),
                        side=str(leg.side),
                        qty=int(leg.qty),
                        filled_qty=int(leg.filled_qty or 0),
                        limit_price=float(leg.limit_price) if leg.limit_price else None,
                        stop_price=float(leg.stop_price) if leg.stop_price else None,
                        created_at=leg.created_at,
                        filled_at=leg.filled_at,
                        filled_avg_price=float(leg.filled_avg_price) if getattr(leg, "filled_avg_price", None) else None,
                    )
                )

        return OrderInfo(
            order_id=str(o.id),
            client_order_id=str(o.client_order_id or ""),
            symbol=o.symbol,
            status=str(o.status),
            side=str(o.side),
            qty=int(o.qty),
            filled_qty=int(o.filled_qty or 0),
            limit_price=float(o.limit_price) if o.limit_price else None,
            stop_price=float(o.stop_price) if o.stop_price else None,
            created_at=o.created_at,
            filled_at=o.filled_at,
            legs=legs,
            filled_avg_price=float(o.filled_avg_price) if getattr(o, "filled_avg_price", None) else None,
        )

    async def get_open_orders(self) -> List[OrderInfo]:
        """Fetch all pending open orders (including bracket legs)."""
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True)
        raw_orders = await asyncio.to_thread(self.client.get_orders, filter=req)
        return [self._parse_order(o) for o in raw_orders]

    async def get_order_by_client_id(self, client_order_id: str) -> Optional[OrderInfo]:
        """Fetches a specific order (with nested legs) by client_order_id."""
        try:
            raw_order = await asyncio.to_thread(self.client.get_order_by_client_id, client_order_id)
            return self._parse_order(raw_order) if raw_order else None
        except Exception as e:
            logger.debug("Order lookup by client_order_id %s failed: %s", client_order_id, e)
            return None

    async def get_closed_orders(self, after: Optional[datetime] = None) -> List[OrderInfo]:
        """Fetches closed orders (with nested legs) for fill & exit price reconciliation."""
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, nested=True, after=after, limit=200)
            raw_orders = await asyncio.to_thread(self.client.get_orders, filter=req)
            return [self._parse_order(o) for o in raw_orders]
        except Exception as e:
            logger.warning("Failed to fetch closed orders from Alpaca: %s", e)
            return []

    async def submit_bracket_order(
        self,
        symbol: str,
        qty: int,
        limit_price: float,
        stop_loss_price: float,
        take_profit_price: float,
        client_order_id: str,
    ) -> Dict[str, Any]:
        """Submits an atomic Marketable-Limit Bracket Order with Stop-Loss and Take-Profit legs."""
        limit_price = round(limit_price, 2)
        stop_loss_price = round(stop_loss_price, 2)
        take_profit_price = round(take_profit_price, 2)

        order_request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
            stop_loss=StopLossRequest(stop_price=stop_loss_price),
            client_order_id=client_order_id,
        )

        order = await asyncio.to_thread(self.client.submit_order, order_data=order_request)

        stop_order_id = None
        tp_order_id = None
        if getattr(order, "legs", None):
            for leg in order.legs:
                if leg.stop_price:
                    stop_order_id = str(leg.id)
                elif leg.limit_price:
                    tp_order_id = str(leg.id)

        return {
            "order_id": str(order.id),
            "client_order_id": str(order.client_order_id),
            "symbol": order.symbol,
            "status": str(order.status),
            "qty": int(order.qty),
            "limit_price": limit_price,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "stop_order_id": stop_order_id,
            "tp_order_id": tp_order_id,
        }

    async def replace_stop_loss(self, order_id: str, new_stop_price: float) -> bool:
        """Replaces an active stop-loss leg with a new stop price (e.g. stepping to breakeven)."""
        try:
            req = ReplaceOrderRequest(stop_price=round(new_stop_price, 2))
            await asyncio.to_thread(self.client.replace_order_by_id, order_id=order_id, req=req)
            logger.info("Successfully stepped stop order %s to %.2f", order_id, new_stop_price)
            return True
        except Exception as e:
            logger.error("Failed to replace stop loss on order %s: %s", order_id, e)
            return False

    async def cancel_order(self, order_id: str) -> bool:
        """Cancels a pending order."""
        try:
            await asyncio.to_thread(self.client.cancel_order_by_id, order_id=order_id)
            return True
        except Exception as e:
            logger.warning("Failed to cancel order %s: %s", order_id, e)
            return False

    async def close_all_positions(self) -> None:
        """Emergency or EOD flatten: closes all open positions and cancels all pending orders."""
        logger.warning("Initiating EOD/Kill-Switch close_all_positions on Alpaca...")
        await asyncio.to_thread(self.client.close_all_positions, cancel_orders=True)

    async def is_market_open(self) -> bool:
        """Checks if regular market hours are currently active."""
        clock = await asyncio.to_thread(self.client.get_clock)
        return bool(clock.is_open)

    async def get_market_close_time(self) -> datetime:
        """Returns the market close time for today in Eastern Time (handles early closes)."""
        now_et = datetime.now(self.tz)
        today_str = now_et.strftime("%Y-%m-%d")

        try:
            calendar = await asyncio.to_thread(
                self.client.get_calendar,
                filters=GetCalendarRequest(start=today_str, end=today_str),
            )
            if calendar and len(calendar) > 0:
                day_info = calendar[0]
                close_time_obj = day_info.close
                # If close_time_obj is time or string
                if isinstance(close_time_obj, time):
                    return datetime.combine(now_et.date(), close_time_obj, tzinfo=self.tz)
                elif isinstance(close_time_obj, str):
                    t = datetime.strptime(close_time_obj, "%H:%M").time()
                    return datetime.combine(now_et.date(), t, tzinfo=self.tz)
        except Exception as e:
            logger.error("Error querying Alpaca calendar: %s. Defaulting to 16:00 ET", e)

        # Default standard close: 16:00 ET
        return datetime.combine(now_et.date(), time(16, 0), tzinfo=self.tz)

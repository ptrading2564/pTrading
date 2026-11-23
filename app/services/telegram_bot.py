"""Telegram Bot Service for push alerts, trade notifications, and Semi-Auto execution approval."""

from typing import Any, Dict, Optional
import httpx
import logging

from app.config import settings

logger = logging.getLogger(__name__)


class TelegramService:
    """Sends push notifications and interactive inline keyboards to Telegram."""

    def __init__(self) -> None:
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.enabled = settings.TELEGRAM_ENABLED and bool(self.token) and bool(self.chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    async def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Sends a Telegram message. Fails gracefully if disabled or on network errors."""
        if not self.enabled:
            logger.info("[Telegram Disabled] %s", text)
            return False

        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(f"{self.base_url}/sendMessage", json=payload)
                if res.status_code == 200:
                    return True
                logger.error("Telegram API error (%d): %s", res.status_code, res.text)
                return False
        except Exception as e:
            logger.error("Failed to send Telegram notification: %s", e)
            return False

    async def notify_signal(
        self,
        signal_id: int,
        symbol: str,
        close_price: float,
        qty: int,
        stop_loss: float,
        take_profit: float,
        semi_auto: bool = False,
    ) -> bool:
        """Sends breakout signal notification. Adds Approve/Reject buttons if Semi-Auto."""
        risk_per_share = close_price - stop_loss
        text = (
            f"🚀 <b>Momentum Breakout Signal</b>\n"
            f"<b>Symbol:</b> <code>{symbol}</code>\n"
            f"<b>Entry Price:</b> ${close_price:.2f}\n"
            f"<b>Qty:</b> {qty} shares (~${qty * close_price:,.2f})\n"
            f"<b>Stop-Loss:</b> ${stop_loss:.2f} (-${risk_per_share:.2f})\n"
            f"<b>Target (+2R):</b> ${take_profit:.2f} (+${2 * risk_per_share:.2f})\n"
        )

        reply_markup = None
        if semi_auto:
            text += "\n<i>Semi-Auto Mode: Awaiting your manual confirmation.</i>"
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Execute Order", "callback_data": f"approve_{signal_id}"},
                        {"text": "❌ Reject", "callback_data": f"reject_{signal_id}"},
                    ]
                ]
            }

        return await self.send_message(text, reply_markup=reply_markup)

    async def notify_order_filled(
        self, symbol: str, qty: int, avg_price: float, stop_loss: float, take_profit: float
    ) -> bool:
        text = (
            f"🟢 <b>Bracket Order Filled</b>\n"
            f"<b>Symbol:</b> <code>{symbol}</code>\n"
            f"<b>Shares:</b> {qty}\n"
            f"<b>Fill Price:</b> ${avg_price:.2f}\n"
            f"<b>Stop-Loss:</b> ${stop_loss:.2f}\n"
            f"<b>Take-Profit:</b> ${take_profit:.2f}\n"
        )
        return await self.send_message(text)

    async def notify_breakeven_stepped(
        self, symbol: str, current_price: float, new_stop: float
    ) -> bool:
        text = (
            f"🛡️ <b>Stop Stepped to Breakeven (+1.0R Reached)</b>\n"
            f"<b>Symbol:</b> <code>{symbol}</code>\n"
            f"<b>Current Price:</b> ${current_price:.2f}\n"
            f"<b>New Stop-Loss:</b> ${new_stop:.2f}\n"
            f"<i>Trade is now risk-free with guaranteed flat/small green exit.</i>"
        )
        return await self.send_message(text)

    async def notify_eod_flatten(self, position_count: int) -> bool:
        text = (
            f"🛑 <b>EOD Flatten Complete</b>\n"
            f"Closed {position_count} open position(s) and cancelled all working orders.\n"
            f"Portfolio is <b>100% FLAT CASH</b> for the overnight session."
        )
        return await self.send_message(text)

    async def notify_circuit_breaker(self, current_drawdown_pct: float) -> bool:
        text = (
            f"🚨 <b>DAILY CIRCUIT BREAKER TRIPPED</b>\n"
            f"<b>Current Drawdown:</b> -{abs(current_drawdown_pct):.2f}%\n"
            f"<b>Threshold:</b> -{settings.DAILY_MAX_LOSS_PERCENT:.2f}%\n"
            f"<i>All automated entries halted for today.</i>"
        )
        return await self.send_message(text)


telegram = TelegramService()

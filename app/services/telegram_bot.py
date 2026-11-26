"""Telegram Bot Service for push alerts, trade notifications, and Semi-Auto execution approval."""

from typing import Any, Dict, Optional, Tuple
import httpx
import logging

from app.config import settings

logger = logging.getLogger(__name__)


class TelegramService:
    """Sends push notifications and interactive inline keyboards to Telegram."""

    def __init__(self) -> None:
        pass

    @property
    def token(self) -> str:
        return settings.TELEGRAM_BOT_TOKEN

    @property
    def chat_id(self) -> str:
        return settings.TELEGRAM_CHAT_ID

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    @property
    def enabled(self) -> bool:
        return bool(settings.TELEGRAM_ENABLED and self.is_configured)

    @property
    def base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    async def set_webhook(self, webhook_url: str) -> Tuple[bool, str]:
        """Registers the Telegram webhook for receiving inline button clicks."""
        if not self.token:
            return False, "Telegram Bot Token is not configured."
        url = f"{self.base_url}/setWebhook"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, json={"url": webhook_url})
                data = res.json()
                if res.status_code == 200 and data.get("ok"):
                    logger.info("Successfully registered Telegram webhook: %s", webhook_url)
                    return True, data.get("description", "Webhook set successfully.")
                err = data.get("description", f"Status {res.status_code}")
                logger.error("Failed to set Telegram webhook: %s", err)
                return False, err
        except Exception as e:
            logger.error("Exception setting Telegram webhook: %s", e)
            return False, str(e)

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

    async def notify_daily_report(
        self, report: Dict[str, Any], diagnostics: Optional[list] = None
    ) -> bool:
        """Sends a concise, scannable End-of-Day comprehensive trading & strategy digest."""
        d_date = report.get("report_date", "")
        roi = report.get("roi_equity", {})
        daily = report.get("daily_trades_metrics", {})
        cum = report.get("cumulative_trades_metrics", {})
        ror = report.get("risk_of_ruin", {})
        proj = report.get("projections", {})

        pnl = float(roi.get("daily_pnl", 0.0))
        pnl_icon = "🟢" if pnl >= 0 else "🔴"
        ror_20 = ror.get("analytical", {}).get("drawdown_probabilities", {}).get("20pct")
        ror_str = f"{ror_20:.2f}%" if ror_20 is not None else "N/A"

        horizons = proj.get("horizons", [])
        proj_line = "Insufficient data"
        if horizons:
            h1y = horizons[-1]
            proj_line = f"${h1y['p50_equity']:,.0f} ({h1y['p50_roi_pct']:+.1f}%)"

        top_notes = ""
        if diagnostics:
            highlights = [
                f"• [{d['severity']}] {d['title']}"
                for d in diagnostics[:3]
            ]
            top_notes = "\n\n<b>Strategy Review Highlights:</b>\n" + "\n".join(highlights)

        health = report.get("health_score", {})
        health_str = f"{health.get('score', 0)}/100 ({health.get('grade', 'N/A')})" if health else "N/A"

        text = (
            f"{pnl_icon} <b>Daily Comprehensive Report ({d_date})</b>\n"
            f"<b>Strategy Health:</b> {health_str}\n"
            f"<b>Daily P&amp;L:</b> ${pnl:+,.2f} ({roi.get('daily_roi_pct', 0.0):+.2f}%)\n"
            f"<b>Equity:</b> ${roi.get('daily_ending_equity', 0.0):,.2f} | <b>Cum ROI:</b> {roi.get('cumulative_roi_pct', 0.0):+.2f}%\n"
            f"<b>Trades Today:</b> {daily.get('total_trades', 0)} ({daily.get('wins', 0)}W/{daily.get('losses', 0)}L/{daily.get('breakevens', 0)}BE | {daily.get('total_r', 0.0):+.2f}R)\n"
            f"<b>Realized R:R:</b> {cum.get('realized_rr_r', 0.0):.2f}:1 | <b>Expectancy:</b> {cum.get('expectancy_r', 0.0):+.2f}R\n"
            f"<b>Risk of Ruin (-20% DD):</b> {ror_str} | <b>PF:</b> {cum.get('profit_factor', 0.0):.2f}\n"
            f"<b>1Y Median Projection:</b> {proj_line}"
            f"{top_notes}\n\n"
            f"<i>Open /analytics for full charts &amp; written strategy review.</i>"
        )
        return await self.send_message(text)


telegram = TelegramService()

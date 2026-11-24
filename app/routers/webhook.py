"""TradingView Webhook ingestion router with IP allowlisting, secret validation, and idempotency."""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
import logging

from app.brokers.alpaca_adapter import AlpacaAdapter
from app.config import settings
from app.db import get_connection, get_system_state, record_signal
from app.services.risk_manager import RiskManager
from app.services.telegram_bot import telegram

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Webhook"])

# Singleton broker and risk manager instances for routing
broker = AlpacaAdapter()
risk_manager = RiskManager(broker)


class SignalPayload(BaseModel):
    symbol: str
    close: float
    or_high: float
    or_low: float
    atr: float
    bar_time: Optional[int] = None
    secret: Optional[str] = None


def verify_client_ip(request: Request) -> None:
    """Verifies that the incoming request originates from TradingView's webhook IP range."""
    if not settings.VERIFY_TRADINGVIEW_IP:
        return

    # Check X-Forwarded-For (from Caddy reverse proxy) or direct client host
    forwarded = request.headers.get("x-forwarded-for")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "")

    if client_ip not in settings.TRADINGVIEW_IPS:
        logger.warning("Rejected webhook from unauthorized IP: %s", client_ip)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: IP {client_ip} is not in TradingView allowlist.",
        )


def verify_secret(payload_secret: Optional[str], header_secret: Optional[str]) -> None:
    """Verifies that the provided secret matches WEBHOOK_SECRET."""
    token = header_secret or payload_secret
    if not token or token != settings.WEBHOOK_SECRET:
        logger.warning("Rejected webhook with invalid or missing secret.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid webhook secret token.",
        )


async def execute_order_for_signal(
    symbol: str,
    qty: int,
    entry_limit_price: float,
    stop_loss_price: float,
    take_profit_price: float,
    bar_time: int,
    signal_price: Optional[float] = None,
    stop_type: Optional[str] = None,
    signal_rvol: Optional[float] = None,
    signal_atr_ext: Optional[float] = None,
    or_width_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """Submits the bracket order to the broker and records order/trade in DB."""
    client_order_id = f"pt_{symbol.lower()}_{bar_time}"

    order_result = await broker.submit_bracket_order(
        symbol=symbol,
        qty=qty,
        limit_price=entry_limit_price,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        client_order_id=client_order_id,
    )

    initial_risk_dollars = round(max(entry_limit_price - stop_loss_price, 0.0) * qty, 2)
    trade_date = datetime.now(ZoneInfo(settings.MARKET_TIMEZONE)).strftime("%Y-%m-%d")

    async with get_connection() as conn:
        # Record into orders table
        await conn.execute(
            """
            INSERT INTO orders (client_order_id, broker_order_id, symbol, side, qty,
                                entry_limit_price, stop_loss_price, take_profit_price,
                                stop_loss_order_id, take_profit_order_id, status)
            VALUES (?, ?, ?, 'buy', ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                client_order_id,
                order_result["order_id"],
                symbol,
                qty,
                entry_limit_price,
                stop_loss_price,
                take_profit_price,
                order_result.get("stop_order_id"),
                order_result.get("tp_order_id"),
                order_result["status"],
            ),
        )

        # Record into trades table with full risk & signal attribution
        await conn.execute(
            """
            INSERT INTO trades (
                client_order_id, symbol, qty, entry_price,
                stop_loss_price, initial_stop_price, initial_risk_dollars,
                take_profit_price, signal_price, stop_type,
                signal_rvol, signal_atr_ext, or_width_pct, trade_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                client_order_id,
                symbol,
                qty,
                entry_limit_price,
                stop_loss_price,
                stop_loss_price,
                initial_risk_dollars,
                take_profit_price,
                signal_price if signal_price is not None else entry_limit_price,
                stop_type,
                signal_rvol,
                signal_atr_ext,
                or_width_pct,
                trade_date,
            ),
        )
        await conn.commit()

    await telegram.notify_order_filled(
        symbol=symbol,
        qty=qty,
        avg_price=entry_limit_price,
        stop_loss=stop_loss_price,
        take_profit=take_profit_price,
    )

    return order_result


@router.post("/tradingview")
async def receive_tradingview_webhook(
    request: Request,
    x_webhook_secret: Optional[str] = Header(None, alias="X-Webhook-Secret"),
) -> Dict[str, Any]:
    """Receives and processes single or consolidated JSON array webhook alerts from TradingView."""
    # 1. IP allowlist check
    verify_client_ip(request)

    # 2. Parse JSON payload (can be dict or list of dicts)
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON payload: {e}",
        )

    items: List[Dict[str, Any]] = body if isinstance(body, list) else [body]
    if not items:
        return {"status": "ok", "message": "Empty signal array received."}

    # 3. Check secret from first item or header
    first_item_secret = items[0].get("secret") if isinstance(items[0], dict) else None
    verify_secret(first_item_secret, x_webhook_secret)

    # 4. Check global system state
    kill_switch = await get_system_state("kill_switch", default="OFF")
    if kill_switch == "ON":
        logger.warning("Signal rejected: Emergency Kill-Switch is ACTIVE.")
        return {"status": "rejected", "reason": "Kill-switch is active."}

    mode_str = await get_system_state("execution_mode", default=settings.EXECUTION_MODE.value)

    results = []

    for item in items:
        try:
            signal = SignalPayload(**item)
        except Exception as e:
            logger.warning("Skipping malformed signal item %s: %s", item, e)
            results.append({"symbol": item.get("symbol", "UNKNOWN"), "status": "malformed", "error": str(e)})
            continue

        bar_time = signal.bar_time or int(request.state.received_time if hasattr(request.state, "received_time") else 0)

        # Idempotency check via unique (bar_time, symbol) in signals table
        signal_id = await record_signal(
            bar_time=bar_time,
            symbol=signal.symbol,
            close_price=signal.close,
            or_high=signal.or_high,
            or_low=signal.or_low,
            atr=signal.atr,
            status="RECEIVED",
            raw_payload=json.dumps(item),
        )

        if signal_id is None:
            # Duplicate signal detected
            logger.info("Duplicate signal ignored for %s at bar_time %s", signal.symbol, bar_time)
            results.append({"symbol": signal.symbol, "status": "duplicate"})
            continue

        # Handle Mode: PAUSED
        if mode_str == "PAUSED":
            logger.info("Signal recorded in PAUSED mode for %s", signal.symbol)
            async with get_connection() as conn:
                await conn.execute("UPDATE signals SET status = 'PAUSED' WHERE id = ?;", (signal_id,))
                await conn.commit()
            results.append({"symbol": signal.symbol, "status": "paused"})
            continue

        # Evaluate Risk Engine
        evaluation = await risk_manager.evaluate_signal(
            symbol=signal.symbol,
            close_price=signal.close,
            or_high=signal.or_high,
            or_low=signal.or_low,
            atr=signal.atr,
        )

        if not evaluation.approved:
            logger.warning("Signal for %s rejected by RiskManager: %s", signal.symbol, evaluation.rejection_reason)
            async with get_connection() as conn:
                await conn.execute(
                    "UPDATE signals SET status = 'REJECTED', rejection_reason = ? WHERE id = ?;",
                    (evaluation.rejection_reason, signal_id),
                )
                await conn.commit()
            results.append({"symbol": signal.symbol, "status": "rejected", "reason": evaluation.rejection_reason})
            continue

        # Handle Mode: SEMI_AUTO (Send Telegram alert with Approve/Reject inline button)
        if mode_str == "SEMI_AUTO":
            async with get_connection() as conn:
                await conn.execute("UPDATE signals SET status = 'AWAITING_APPROVAL' WHERE id = ?;", (signal_id,))
                await conn.commit()

            await telegram.notify_signal(
                signal_id=signal_id,
                symbol=signal.symbol,
                close_price=evaluation.entry_limit_price,
                qty=evaluation.qty,
                stop_loss=evaluation.stop_loss_price,
                take_profit=evaluation.take_profit_price,
                semi_auto=True,
            )
            results.append({"symbol": signal.symbol, "status": "awaiting_approval", "signal_id": signal_id})
            continue

        # Handle Mode: AUTO (Direct Execution)
        if mode_str == "AUTO":
            try:
                order_result = await execute_order_for_signal(
                    symbol=signal.symbol,
                    qty=evaluation.qty,
                    entry_limit_price=evaluation.entry_limit_price,
                    stop_loss_price=evaluation.stop_loss_price,
                    take_profit_price=evaluation.take_profit_price,
                    bar_time=bar_time,
                )
                async with get_connection() as conn:
                    await conn.execute("UPDATE signals SET status = 'EXECUTED' WHERE id = ?;", (signal_id,))
                    await conn.commit()

                results.append({"symbol": signal.symbol, "status": "executed", "order": order_result})
            except Exception as e:
                logger.error("Failed to execute order for %s: %s", signal.symbol, e)
                async with get_connection() as conn:
                    await conn.execute(
                        "UPDATE signals SET status = 'FAILED', rejection_reason = ? WHERE id = ?;",
                        (str(e), signal_id),
                    )
                    await conn.commit()
                results.append({"symbol": signal.symbol, "status": "failed", "error": str(e)})

    return {"status": "success", "processed": len(results), "results": results}


@router.post("/telegram")
async def telegram_webhook(request: Request) -> Dict[str, Any]:
    """Handles Telegram inline callback approvals (e.g. Semi-Auto execute/reject)."""
    try:
        update = await request.json()
    except Exception:
        return {"ok": False}

    callback_query = update.get("callback_query")
    if not callback_query:
        return {"ok": True}

    data = callback_query.get("data", "")
    callback_id = callback_query.get("id")

    if data.startswith("approve_"):
        signal_id = int(data.replace("approve_", ""))
        async with get_connection() as conn:
            cursor = await conn.execute("SELECT * FROM signals WHERE id = ?;", (signal_id,))
            sig = await cursor.fetchone()

            if sig and sig["status"] == "AWAITING_APPROVAL":
                evaluation = await risk_manager.evaluate_signal(
                    symbol=sig["symbol"],
                    close_price=sig["close_price"],
                    or_high=sig["or_high"],
                    or_low=sig["or_low"],
                    atr=sig["atr"],
                )
                if evaluation.approved:
                    await execute_order_for_signal(
                        symbol=sig["symbol"],
                        qty=evaluation.qty,
                        entry_limit_price=evaluation.entry_limit_price,
                        stop_loss_price=evaluation.stop_loss_price,
                        take_profit_price=evaluation.take_profit_price,
                        bar_time=sig["bar_time"],
                    )
                    await conn.execute("UPDATE signals SET status = 'EXECUTED' WHERE id = ?;", (signal_id,))
                    await conn.commit()
                    await telegram.send_message(f"✅ Approved and executed order for <b>{sig['symbol']}</b>.")

    elif data.startswith("reject_"):
        signal_id = int(data.replace("reject_", ""))
        async with get_connection() as conn:
            await conn.execute("UPDATE signals SET status = 'REJECTED_BY_USER' WHERE id = ?;", (signal_id,))
            await conn.commit()
        await telegram.send_message(f"❌ Signal #{signal_id} rejected.")

    return {"ok": True}

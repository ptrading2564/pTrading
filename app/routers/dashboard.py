"""Dashboard router: Web UI, Authentication, Mode Switcher, Kill-Switch, and ChartMill CSV Upload."""

import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
import logging

from app.brokers.alpaca_adapter import AlpacaAdapter
from app.config import ExecutionMode, settings
from app.db import get_connection, get_recent_signals, get_system_state, set_system_state
from app.services.chartmill_importer import chartmill_importer
from app.services.market_scanner import get_market_scanner
from app.services.risk_manager import RiskManager
from app.services.telegram_bot import telegram

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Dashboard"])
security = HTTPBasic()

templates_path = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))

broker = AlpacaAdapter()
risk_manager = RiskManager(broker)


def authenticate_user(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Verifies HTTP Basic authentication credentials against configured settings."""
    correct_username = secrets.compare_digest(credentials.username, settings.DASHBOARD_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, settings.DASHBOARD_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@router.get("/health")
async def health_check() -> dict:
    """System health check endpoint."""
    broker_ok = False
    equity = 0.0
    try:
        equity = await broker.get_account_equity()
        broker_ok = True
    except Exception as e:
        logger.warning("Health check broker query failed: %s", e)

    mode = await get_system_state("execution_mode", default=settings.EXECUTION_MODE.value)
    kill_switch = await get_system_state("kill_switch", default="OFF")

    return {
        "status": "healthy" if broker_ok else "degraded",
        "timestamp_utc": datetime.utcnow().isoformat(),
        "environment": settings.ENVIRONMENT,
        "broker_connected": broker_ok,
        "live_equity": equity,
        "execution_mode": mode,
        "kill_switch": kill_switch,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard_home(
    request: Request,
    username: str = Depends(authenticate_user),
    basket_str: Optional[str] = None,
    basket_count: Optional[int] = None,
):
    """Main responsive dashboard rendering live positions, controls, and signals."""
    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    now_et = datetime.now(tz)
    current_time_et = now_et.strftime("%H:%M:%S")

    # Fetch live state
    try:
        equity = await broker.get_account_equity()
        positions = await broker.get_positions()
        is_market_open = await broker.is_market_open()
        market_close = await broker.get_market_close_time()
        flatten_time_et = (market_close).strftime("%H:%M")
    except Exception as e:
        logger.error("Error fetching broker data for dashboard: %s", e)
        equity = 50000.0
        positions = []
        is_market_open = False
        flatten_time_et = settings.EOD_FLATTEN_TIME

    starting_equity, breaker_tripped = await risk_manager.get_or_initialize_daily_stats(equity)
    daily_pnl = equity - starting_equity
    daily_pnl_pct = (daily_pnl / starting_equity) * 100.0 if starting_equity > 0 else 0.0

    mode = await get_system_state("execution_mode", default=settings.EXECUTION_MODE.value)
    kill_switch = await get_system_state("kill_switch", default="OFF")
    recent_signals = await get_recent_signals(limit=25)

    saved_basket = await get_system_state("latest_chartmill_basket")
    active_basket_str = basket_str or saved_basket or ""
    active_basket_count = basket_count or (len(active_basket_str.split(",")) if active_basket_str else 0)

    # Fetch server-side scanner status (Option 3)
    try:
        scanner_info = get_market_scanner().get_status_info()
    except Exception as e:
        logger.warning("Error fetching scanner status info: %s", e)
        scanner_info = {"status": "UNAVAILABLE", "market_health": {}}

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "username": username,
            "equity": equity,
            "starting_equity": starting_equity,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl_pct,
            "daily_breaker_limit": settings.DAILY_MAX_LOSS_PERCENT,
            "positions": positions,
            "max_positions": settings.MAX_CONCURRENT_POSITIONS,
            "is_market_open": is_market_open,
            "current_time_et": current_time_et,
            "flatten_time_et": flatten_time_et,
            "execution_mode": mode,
            "kill_switch": kill_switch,
            "recent_signals": recent_signals,
            "basket_string": active_basket_str,
            "basket_count": active_basket_count,
            "scanner_info": scanner_info,
        },
    )


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request, username: str = Depends(authenticate_user)):
    """Historical analytics, daily equity logs, and trade outcomes."""
    async with get_connection() as conn:
        cursor_daily = await conn.execute("SELECT * FROM daily_stats ORDER BY date DESC LIMIT 30;")
        daily_records = [dict(r) for r in await cursor_daily.fetchall()]

        cursor_trades = await conn.execute(
            "SELECT * FROM trades WHERE is_open = 0 ORDER BY exit_time DESC LIMIT 50;"
        )
        closed_trades = [dict(r) for r in await cursor_trades.fetchall()]

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            "username": username,
            "daily_records": daily_records,
            "closed_trades": closed_trades,
            "is_market_open": False,
            "current_time_et": datetime.now(ZoneInfo(settings.MARKET_TIMEZONE)).strftime("%H:%M:%S"),
        },
    )


@router.post("/api/system/mode")
async def update_execution_mode(
    mode: ExecutionMode = Form(...),
    username: str = Depends(authenticate_user),
):
    """Changes system execution mode (AUTO, SEMI_AUTO, PAUSED)."""
    await set_system_state("execution_mode", mode.value)
    logger.info("User %s updated execution mode to %s", username, mode.value)
    await telegram.send_message(f"⚙️ Execution Mode changed to <b>{mode.value}</b> by {username}.")
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/system/kill-switch")
async def toggle_kill_switch(
    action: str = Form(...),
    username: str = Depends(authenticate_user),
):
    """Emergency Flatten Kill-Switch."""
    if action == "ARM":
        await set_system_state("kill_switch", "ON")
        await set_system_state("execution_mode", ExecutionMode.PAUSED.value)
        logger.critical("EMERGENCY KILL-SWITCH ARMED BY %s! Flattening positions...", username)

        # Trigger immediate broker flatten
        try:
            await broker.close_all_positions()
        except Exception as e:
            logger.error("Error executing emergency flatten on broker: %s", e)

        # Mark open trades as closed by KILL_SWITCH
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE trades
                SET is_open = 0, exit_time = CURRENT_TIMESTAMP, exit_reason = 'KILL_SWITCH'
                WHERE is_open = 1;
                """
            )
            await conn.commit()

        await telegram.send_message(f"🚨 <b>EMERGENCY KILL-SWITCH TRIGGERED by {username}!</b> All positions flattened.")

    elif action == "DISARM":
        await set_system_state("kill_switch", "OFF")
        logger.info("Kill-Switch disarmed by %s.", username)
        await telegram.send_message(f"✅ Kill-Switch disarmed by {username}. System remains paused until mode is set.")

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/chartmill/upload")
async def upload_chartmill_csv(
    file: UploadFile = File(...),
    username: str = Depends(authenticate_user),
):
    """Uploads and processes ChartMill CSV export, filters earnings, and creates the top-38 basket."""
    content = (await file.read()).decode("utf-8", errors="ignore")
    result = chartmill_importer.parse_csv(content)

    # Save latest basket string to DB
    await set_system_state("latest_chartmill_basket", result["basket_string"])

    logger.info(
        "ChartMill CSV processed: %d total, %d qualified, %d earnings excluded, %d in basket.",
        result["total_imported"],
        result["qualified_count"],
        result["earnings_filtered_count"],
        result["basket_size"],
    )

    await telegram.send_message(
        f"📊 <b>ChartMill Basket Generated:</b> {result['basket_size']} stocks ranked for today.\n"
        f"<i>Earnings Filtered: {result['earnings_filtered_count']} stocks</i>"
    )

    return RedirectResponse(
        url=f"/?basket_str={result['basket_string']}&basket_count={result['basket_size']}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/api/signals/approve")
async def approve_signal(
    signal_id: int = Form(...),
    username: str = Depends(authenticate_user),
):
    """Semi-Auto manual approval from Web Dashboard."""
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
                from app.routers.webhook import execute_order_for_signal
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
                await telegram.send_message(f"✅ Approved and executed order for <b>{sig['symbol']}</b> from Web UI.")

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/signals/reject")
async def reject_signal(
    signal_id: int = Form(...),
    username: str = Depends(authenticate_user),
):
    """Semi-Auto rejection from Web Dashboard."""
    async with get_connection() as conn:
        await conn.execute("UPDATE signals SET status = 'REJECTED_BY_USER' WHERE id = ?;", (signal_id,))
        await conn.commit()
    await telegram.send_message(f"❌ Signal #{signal_id} rejected from Web UI.")
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/scanner/run-now")
async def trigger_scanner_manual(username: str = Depends(authenticate_user)):
    """Manually triggers an immediate server-side scan iteration."""
    scanner = get_market_scanner()
    results = await scanner.scan_once()
    logger.info("Manual scan triggered by %s. Results: %d", username, len(results))
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


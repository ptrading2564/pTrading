"""Dashboard router: Web UI, Authentication, Mode Switcher, Kill-Switch, and ChartMill CSV Upload."""

import csv
import hashlib
import hmac
import io
import json
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
import logging

from app.brokers.alpaca_adapter import AlpacaAdapter
from app.config import ExecutionMode, settings
from app.db import (
    get_chartmill_profile,
    get_connection,
    get_daily_report,
    get_recent_signals,
    get_system_state,
    list_daily_report_dates,
    save_chartmill_universe,
    save_daily_report,
    set_system_state,
)
from app.services.analytics_service import analytics_service
from app.services.chartmill_importer import chartmill_importer
from app.services.lifecycle_worker import LifecycleWorker
from app.services.market_scanner import get_market_scanner
from app.services.risk_manager import RiskManager
from app.services.strategy_reviewer import strategy_reviewer
from app.services.telegram_bot import telegram

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Dashboard"])
security = HTTPBasic(auto_error=False)

SESSION_COOKIE_NAME = "ptrading_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days

templates_path = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))

broker = AlpacaAdapter()
risk_manager = RiskManager(broker)


def _create_session_token(username: str) -> str:
    """Creates an HMAC-SHA256 signed session token valid for 7 days."""
    exp = int(time.time()) + SESSION_MAX_AGE_SECONDS
    payload = f"{username}:{exp}"
    sig = hmac.new(
        settings.SESSION_SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}:{sig}"


def _verify_session_token(token: Optional[str]) -> Optional[str]:
    """Verifies an HMAC-SHA256 signed session cookie token and returns the username if valid."""
    if not token:
        return None
    parts = token.split(":")
    if len(parts) != 3:
        return None
    username, exp_str, sig = parts
    try:
        if int(exp_str) < int(time.time()):
            return None
    except ValueError:
        return None
    expected_sig = hmac.new(
        settings.SESSION_SECRET_KEY.encode("utf-8"),
        f"{username}:{exp_str}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if secrets.compare_digest(sig, expected_sig) and secrets.compare_digest(username, settings.DASHBOARD_USERNAME):
        return username
    return None


def authenticate_user(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> str:
    """Authenticates via signed session cookie (HTML login) OR HTTP Basic Auth (API/scripts)."""
    # 1. Check signed session cookie first
    cookie_user = _verify_session_token(request.cookies.get(SESSION_COOKIE_NAME))
    if cookie_user:
        return cookie_user

    # 2. Check HTTP Basic Auth credentials
    if credentials is not None:
        correct_username = secrets.compare_digest(credentials.username, settings.DASHBOARD_USERNAME)
        correct_password = secrets.compare_digest(credentials.password, settings.DASHBOARD_PASSWORD)
        if correct_username and correct_password:
            return credentials.username

    # 3. Redirect interactive browser HTML requests to /login; return 401 for API requests
    accept = request.headers.get("accept", "")
    is_api = request.url.path.startswith("/api/")
    if not is_api and "text/html" in accept:
        next_path = request.url.path
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/login?next={quote(next_path, safe='/?=&')}"},
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username or password",
        headers={"WWW-Authenticate": "Basic"},
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    """Renders the clean HTML sign-in page."""
    if _verify_session_token(request.cookies.get(SESSION_COOKIE_NAME)):
        return RedirectResponse(url=next if next.startswith("/") else "/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "next_url": next if next.startswith("/") else "/"},
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    """Validates credentials and sets an HttpOnly signed session cookie."""
    correct_username = secrets.compare_digest(username.strip(), settings.DASHBOARD_USERNAME)
    correct_password = secrets.compare_digest(password, settings.DASHBOARD_PASSWORD)
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"

    if not (correct_username and correct_password):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid username or password.", "next_url": safe_next},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = RedirectResponse(url=safe_next, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=_create_session_token(username.strip()),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https"),
    )
    return response


@router.get("/logout")
async def logout():
    """Clears the session cookie and redirects to /login."""
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return response


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
        flatten_time_et = (market_close - timedelta(minutes=5)).strftime("%H:%M")
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
    for sig in recent_signals:
        raw_ts = sig.get("received_at")
        if raw_ts:
            try:
                dt_utc = datetime.strptime(str(raw_ts)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
                sig["received_at"] = dt_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S ET")
            except Exception:
                pass

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


async def _load_or_build_report(report_date: Optional[str] = None, force_refresh: bool = False) -> dict:
    """Loads a stored daily report or generates it on demand."""
    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    target_date = report_date or datetime.now(tz).strftime("%Y-%m-%d")

    if not force_refresh:
        stored = await get_daily_report(target_date)
        if stored and stored.get("metrics"):
            metrics = stored["metrics"]
            diagnostics = metrics.get("diagnostics") or strategy_reviewer.run_diagnostics(metrics)
            return {
                "report": metrics,
                "review_markdown": stored["review_markdown"],
                "review_source": stored.get("review_source", "deterministic"),
                "diagnostics": diagnostics,
                "generated_at": stored.get("generated_at"),
            }

    report = await analytics_service.build_comprehensive_report(report_date=target_date)
    review_md, review_source, diagnostics = await strategy_reviewer.generate_review(report)
    report["diagnostics"] = diagnostics

    if force_refresh:
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
        "generated_at": report.get("generated_at"),
    }


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    date: Optional[str] = None,
    username: str = Depends(authenticate_user),
):
    """Comprehensive daily & cumulative analytics, Risk of Ruin, projections, and written strategy review."""
    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    now_et = datetime.now(tz)

    async with get_connection() as conn:
        cursor_daily = await conn.execute("SELECT * FROM daily_stats ORDER BY date DESC LIMIT 60;")
        daily_records = [dict(r) for r in await cursor_daily.fetchall()]

        cursor_trades = await conn.execute(
            """
            SELECT * FROM trades
            WHERE is_open = 0 AND exit_price IS NOT NULL AND pnl_dollars IS NOT NULL
            ORDER BY COALESCE(exit_time, entry_time) DESC LIMIT 100;
            """
        )
        closed_trades = [dict(r) for r in await cursor_trades.fetchall()]

    stored_dates = await list_daily_report_dates(limit=60)
    daily_dates = [d["date"] for d in daily_records]
    available_dates = sorted(set(stored_dates + daily_dates + [now_et.strftime("%Y-%m-%d")]), reverse=True)
    selected_date = date or (daily_dates[0] if daily_dates else now_et.strftime("%Y-%m-%d"))

    bundle = await _load_or_build_report(report_date=selected_date, force_refresh=False)

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            "username": username,
            "selected_date": selected_date,
            "available_dates": available_dates,
            "report": bundle["report"],
            "report_json": json.dumps(bundle["report"]),
            "review_markdown": bundle["review_markdown"],
            "review_source": bundle["review_source"],
            "diagnostics": bundle["diagnostics"],
            "daily_records": daily_records,
            "closed_trades": closed_trades,
            "is_market_open": False,
            "current_time_et": now_et.strftime("%H:%M:%S"),
        },
    )


@router.get("/api/analytics/report")
async def get_analytics_report_json(
    date: Optional[str] = None,
    username: str = Depends(authenticate_user),
):
    """Returns the full comprehensive daily & cumulative analytics report as JSON."""
    bundle = await _load_or_build_report(report_date=date, force_refresh=False)
    return JSONResponse(
        content={
            "report": bundle["report"],
            "diagnostics": bundle["diagnostics"],
            "review_source": bundle["review_source"],
            "review_markdown": bundle["review_markdown"],
        }
    )


@router.get("/api/analytics/review.md")
async def download_strategy_review_markdown(
    date: Optional[str] = None,
    username: str = Depends(authenticate_user),
):
    """Downloads the written Daily Strategy Review as a Markdown file."""
    bundle = await _load_or_build_report(report_date=date, force_refresh=False)
    d_str = bundle["report"]["report_date"]
    return PlainTextResponse(
        content=bundle["review_markdown"],
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="ptrading_review_{d_str}.md"'},
    )


@router.get("/api/analytics/export/csv")
async def export_trades_csv(username: str = Depends(authenticate_user)):
    """Exports all closed trades with full risk, slippage, MFE/MAE, and P&L metrics as CSV."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM trades WHERE is_open = 0 ORDER BY COALESCE(exit_time, entry_time) DESC;"
        )
        trades = [dict(r) for r in await cursor.fetchall()]

    buf = io.StringIO()
    fields = [
        "trade_date", "symbol", "qty", "entry_price", "exit_price",
        "initial_stop_price", "take_profit_price", "initial_risk_dollars",
        "pnl_dollars", "pnl_r", "exit_reason", "breakeven_stepped",
        "stop_type", "signal_rvol", "signal_atr_ext", "slippage_dollars",
        "slippage_r", "mfe_r", "mae_r", "post_exit_max_r", "entry_time", "exit_time",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for t in trades:
        writer.writerow(t)

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="ptrading_trades_export.csv"'},
    )


@router.post("/api/analytics/generate")
async def generate_analytics_report_manual(
    date: Optional[str] = Form(None),
    username: str = Depends(authenticate_user),
):
    """Manually backfills missing broker fills, enriches MFE/MAE, and refreshes the daily report."""
    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    target_date = date or datetime.now(tz).strftime("%Y-%m-%d")
    try:
        worker = LifecycleWorker(broker)
        await worker.backfill_missing_trade_exits()
        await worker.update_daily_stats_snapshot()
    except Exception as e:
        logger.warning("Manual trade fill backfill skipped: %s", e)

    try:
        await analytics_service.enrich_eod_excursions_and_regime(target_date)
    except Exception as e:
        logger.debug("Manual enrichment skipped: %s", e)

    await _load_or_build_report(report_date=target_date, force_refresh=True)
    return RedirectResponse(url=f"/analytics?date={target_date}", status_code=status.HTTP_303_SEE_OTHER)


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

    # Save latest basket string and per-symbol ChartMill technical/fundamental scorecards to DB
    await set_system_state("latest_chartmill_basket", result["basket_string"])
    today_et = datetime.now(ZoneInfo(settings.MARKET_TIMEZONE)).strftime("%Y-%m-%d")
    await save_chartmill_universe(today_et, result["basket"])

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


@router.get("/api/dashboard/live")
async def get_dashboard_live_state(username: str = Depends(authenticate_user)):
    """Lightweight JSON polling endpoint for dynamic in-place dashboard auto-refresh."""
    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    now_et = datetime.now(tz)

    try:
        equity = await broker.get_account_equity()
        positions = await broker.get_positions()
        is_market_open = await broker.is_market_open()
    except Exception as e:
        logger.debug("Live dashboard poll broker fallback: %s", e)
        equity = 50000.0
        positions = []
        is_market_open = False

    starting_equity, _ = await risk_manager.get_or_initialize_daily_stats(equity)
    daily_pnl = round(equity - starting_equity, 2)
    daily_pnl_pct = round((daily_pnl / starting_equity) * 100.0, 2) if starting_equity > 0 else 0.0

    recent_signals = await get_recent_signals(limit=25)
    for sig in recent_signals:
        raw_ts = sig.get("received_at")
        if raw_ts:
            try:
                dt_utc = datetime.strptime(str(raw_ts)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
                sig["received_at"] = dt_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S ET")
            except Exception:
                pass

    try:
        scanner_info = get_market_scanner().get_status_info()
    except Exception:
        scanner_info = {"status": "UNAVAILABLE", "market_health": {}}

    return JSONResponse(
        content={
            "current_time_et": now_et.strftime("%H:%M:%S"),
            "today_date": now_et.strftime("%Y-%m-%d"),
            "is_market_open": is_market_open,
            "equity": round(equity, 2),
            "starting_equity": round(starting_equity, 2),
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl_pct,
            "max_positions": settings.MAX_CONCURRENT_POSITIONS,
            "scanner_info": scanner_info,
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "entry_price": round(float(p.entry_price), 2),
                    "current_price": round(float(p.current_price), 2),
                    "market_value": round(float(p.market_value), 2),
                    "unrealized_pnl": round(float(p.unrealized_pnl), 2),
                }
                for p in positions
            ],
            "recent_signals": [
                {
                    "id": s["id"],
                    "received_at": s["received_at"],
                    "symbol": s["symbol"],
                    "close_price": round(float(s["close_price"]), 2),
                    "or_low": round(float(s["or_low"]), 2),
                    "or_high": round(float(s["or_high"]), 2),
                    "atr": round(float(s["atr"]), 2),
                    "status": s["status"],
                    "rejection_reason": s.get("rejection_reason") or "",
                }
                for s in recent_signals
            ],
        }
    )


@router.get("/api/chart/{symbol}")
async def get_symbol_chart_data(
    symbol: str,
    date: Optional[str] = None,
    username: str = Depends(authenticate_user),
):
    """Returns 5m Alpaca candlesticks, running VWAP, OR High/Low, and trade overlays for a symbol."""
    from datetime import time as dt_time

    clean_sym = "".join(ch for ch in symbol.upper().strip() if ch.isalnum() or ch in (".", "-"))
    if not clean_sym:
        raise HTTPException(status_code=400, detail="Invalid symbol")

    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    now_et = datetime.now(tz)
    target_date_str = date or now_et.strftime("%Y-%m-%d")
    try:
        target_day = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    except ValueError:
        target_day = now_et.date()
        target_date_str = target_day.strftime("%Y-%m-%d")

    session_start = datetime.combine(target_day, dt_time(9, 30), tzinfo=tz)
    session_end = datetime.combine(target_day, dt_time(16, 0), tzinfo=tz)

    scanner = get_market_scanner()
    bars = []
    try:
        bars_map = await scanner.fetch_bars_multi([clean_sym], session_start, session_end)
        bars = bars_map.get(clean_sym, [])
        # If empty (e.g., pre-market or weekend), fall back to most recent trading session in last 5 days
        if not bars:
            fallback_start = datetime.combine(target_day - timedelta(days=5), dt_time(9, 30), tzinfo=tz)
            fb_map = await scanner.fetch_bars_multi([clean_sym], fallback_start, session_end)
            fb_bars = fb_map.get(clean_sym, [])
            if fb_bars:
                last_day = fb_bars[-1].timestamp.astimezone(tz).date()
                target_date_str = last_day.strftime("%Y-%m-%d")
                bars = [b for b in fb_bars if b.timestamp.astimezone(tz).date() == last_day]
    except Exception as e:
        logger.warning("Failed to fetch chart bars for %s: %s", clean_sym, e)

    candles = []
    vwap_series = []
    cum_vol = 0.0
    cum_tp_vol = 0.0
    or_high = None
    or_low = None

    for b in bars:
        bt_et = b.timestamp.astimezone(tz)
        # Shift epoch by ET UTC offset so Lightweight Charts displays Eastern Time on the X-axis
        offset_sec = int(bt_et.utcoffset().total_seconds()) if bt_et.utcoffset() else 0
        ts = int(b.timestamp.timestamp()) + offset_sec
        o_p, h_p, l_p, c_p = float(b.open), float(b.high), float(b.low), float(b.close)
        vol = float(b.volume or 0)

        candles.append({"time": ts, "open": round(o_p, 2), "high": round(h_p, 2), "low": round(l_p, 2), "close": round(c_p, 2)})

        if vol > 0:
            tp = (h_p + l_p + c_p) / 3.0
            cum_vol += vol
            cum_tp_vol += tp * vol
        vwap_val = (cum_tp_vol / cum_vol) if cum_vol > 0 else c_p
        vwap_series.append({"time": ts, "value": round(vwap_val, 2)})

        if dt_time(9, 30) <= bt_et.time() < dt_time(9, 45):
            or_high = h_p if or_high is None else max(or_high, h_p)
            or_low = l_p if or_low is None else min(or_low, l_p)

    # Fetch matching trade & signal overlays from DB for this symbol and date
    trade_overlay = None
    async with get_connection() as conn:
        t_row = await (
            await conn.execute(
                """
                SELECT entry_price, stop_loss_price, initial_stop_price, take_profit_price,
                       exit_price, exit_reason, pnl_dollars, pnl_r, is_open, qty
                FROM trades
                WHERE symbol = ? AND COALESCE(trade_date, substr(entry_time, 1, 10)) = ?
                ORDER BY id DESC LIMIT 1;
                """,
                (clean_sym, target_date_str),
            )
        ).fetchone()
        if t_row:
            trade_overlay = dict(t_row)

        if or_high is None or or_low is None:
            s_row = await (
                await conn.execute(
                    """
                    SELECT or_high, or_low FROM signals
                    WHERE symbol = ? ORDER BY id DESC LIMIT 1;
                    """,
                    (clean_sym,),
                )
            ).fetchone()
            if s_row:
                or_high = float(s_row["or_high"])
                or_low = float(s_row["or_low"])

    cm_profile = await get_chartmill_profile(clean_sym, target_date_str)

    return JSONResponse(
        content={
            "symbol": clean_sym,
            "date": target_date_str,
            "candles": candles,
            "vwap": vwap_series,
            "or_high": round(or_high, 2) if or_high is not None else None,
            "or_low": round(or_low, 2) if or_low is not None else None,
            "trade": trade_overlay,
            "chartmill": cm_profile,
            "tradingview_url": f"https://www.tradingview.com/chart/?symbol={clean_sym}",
            "chartmill_url": f"https://www.chartmill.com/stock/quote/{clean_sym}",
            "chartmill_ta_url": f"https://www.chartmill.com/stock/quote/{clean_sym}/technical-analysis",
            "chartmill_fa_url": f"https://www.chartmill.com/stock/quote/{clean_sym}/fundamental-analysis",
        }
    )


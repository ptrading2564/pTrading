"""Dashboard router: Web UI, Authentication, Mode Switcher, Kill-Switch, and ChartMill CSV Upload."""

import base64
import csv
import hashlib
import hmac
import io
import json
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo
import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
import logging

from app.brokers.alpaca_adapter import AlpacaAdapter
from app.config import ExecutionMode, settings
from app.db import (
    add_calendar_event,
    add_capital_ledger_entry,
    delete_calendar_event,
    get_chartmill_profile,
    get_connection,
    get_daily_report,
    get_recent_signals,
    get_system_state,
    list_daily_report_dates,
    save_chartmill_universe,
    save_daily_report,
    set_system_state,
    set_validation_check,
    update_financing_config,
)
from app.services.analytics_service import analytics_service
from app.services.calendar_service import CalendarService
from app.services.chartmill_importer import chartmill_importer
from app.services.financing_service import (
    FLOW_DIRECTIONS,
    RISK_PROFILES,
    VALID_LEDGER_FLOW_TYPES,
    VALID_LIFECYCLE_STAGES,
    financing_service,
)
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
OAUTH_STATE_COOKIE = "ptrading_oauth_state"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

templates_path = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))

broker = AlpacaAdapter()
risk_manager = RiskManager(broker)
calendar_service = CalendarService(broker=broker)


def _is_valid_username(username: str) -> bool:
    """Validates whether a username matches default DASHBOARD_USERNAME or allowed Google emails."""
    if not username:
        return False
    u = username.strip().lower()
    return secrets.compare_digest(u, settings.DASHBOARD_USERNAME.lower()) or (u in settings.allowed_google_emails_set)


def _hash_password(plain_password: str, salt: Optional[str] = None) -> str:
    """Creates a PBKDF2-HMAC-SHA256 salted hash of a password."""
    if not salt:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return f"pbkdf2_sha256${salt}${h.hex()}"


def _verify_password_hash(plain_password: str, stored_hash: str) -> bool:
    """Verifies a plain password against a PBKDF2-HMAC-SHA256 stored hash."""
    try:
        parts = stored_hash.split("$")
        if len(parts) == 3 and parts[0] == "pbkdf2_sha256":
            salt = parts[1]
            expected_hash = parts[2]
            calculated = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
            return secrets.compare_digest(calculated, expected_hash)
    except Exception as e:
        logger.warning("Error verifying password hash: %s", e)
    return False


async def _check_dashboard_password(password_to_check: str) -> bool:
    """Checks password against custom SQLite hash override if set, else falls back to .env."""
    try:
        custom_hash = await get_system_state("dashboard_password_hash")
        if custom_hash:
            return _verify_password_hash(password_to_check, custom_hash)
    except Exception as e:
        logger.warning("Could not check custom password hash from system_state: %s", e)
    return secrets.compare_digest(password_to_check, settings.DASHBOARD_PASSWORD)


def _create_session_token(user_data: Any) -> str:
    """Creates an HMAC-SHA256 signed session token valid for 7 days."""
    exp = int(time.time()) + SESSION_MAX_AGE_SECONDS
    if isinstance(user_data, dict):
        payload_dict = {
            "user": user_data.get("user", settings.DASHBOARD_USERNAME),
            "name": user_data.get("name", user_data.get("user", "")),
            "avatar": user_data.get("avatar", ""),
            "exp": exp,
        }
    else:
        payload_dict = {
            "user": str(user_data),
            "name": str(user_data).split("@")[0].capitalize(),
            "avatar": "",
            "exp": exp,
        }
    payload_json = json.dumps(payload_dict, separators=(",", ":"))
    b64_payload = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("utf-8")
    sig = hmac.new(
        settings.SESSION_SECRET_KEY.encode("utf-8"),
        b64_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"v2.{b64_payload}.{sig}"


def _verify_session_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Verifies HMAC-SHA256 signed session cookie token and returns user dict if valid."""
    if not token:
        return None

    if token.startswith("v2."):
        parts = token.split(".")
        if len(parts) != 3:
            return None
        _, b64_payload, sig = parts
        expected_sig = hmac.new(
            settings.SESSION_SECRET_KEY.encode("utf-8"),
            b64_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not secrets.compare_digest(sig, expected_sig):
            return None
        try:
            payload_json = base64.urlsafe_b64decode(b64_payload.encode("utf-8")).decode("utf-8")
            data = json.loads(payload_json)
            exp = int(data.get("exp", 0))
            if exp < int(time.time()):
                return None
            username = data.get("user", "")
            if not _is_valid_username(username):
                return None
            return {
                "user": username,
                "name": data.get("name") or username.split("@")[0].capitalize(),
                "avatar": data.get("avatar", ""),
            }
        except Exception:
            return None

    # Legacy token fallback: "username:exp:sig"
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
    if secrets.compare_digest(sig, expected_sig) and _is_valid_username(username):
        return {
            "user": username,
            "name": username.split("@")[0].capitalize(),
            "avatar": "",
        }
    return None


async def authenticate_user(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> str:
    """Authenticates via signed session cookie (HTML login) OR HTTP Basic Auth (API/scripts)."""
    # 1. Check signed session cookie first
    session_data = _verify_session_token(request.cookies.get(SESSION_COOKIE_NAME))
    if session_data:
        request.state.user = session_data
        return session_data["user"]

    # 2. Check HTTP Basic Auth credentials
    if credentials is not None:
        valid_user = _is_valid_username(credentials.username)
        valid_pass = await _check_dashboard_password(credentials.password)
        if valid_user and valid_pass:
            user_dict = {
                "user": credentials.username,
                "name": credentials.username.split("@")[0].capitalize(),
                "avatar": "",
            }
            request.state.user = user_dict
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
async def login_page(
    request: Request,
    next: str = "/",
    error: Optional[str] = None,
):
    """Renders the hybrid HTML sign-in page."""
    if _verify_session_token(request.cookies.get(SESSION_COOKIE_NAME)):
        return RedirectResponse(url=next if next.startswith("/") else "/", status_code=status.HTTP_303_SEE_OTHER)

    error_message = None
    if error == "unauthorized_email":
        error_message = "Your Google account is not authorized to access pTrading. Only registered administrator accounts are allowed."
    elif error == "state_mismatch":
        error_message = "Sign-in session expired or security verification failed. Please try again."
    elif error == "oauth_failed":
        error_message = "Google authentication failed or was cancelled. Please try again or use your fallback password."
    elif error:
        error_message = error

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": error_message,
            "next_url": next if next.startswith("/") else "/",
            "google_oauth_enabled": bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_OAUTH_ENABLED),
        },
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    """Validates credentials and sets an HttpOnly signed session cookie."""
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    valid_user = _is_valid_username(username)
    valid_pass = await _check_dashboard_password(password)

    if not (valid_user and valid_pass):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Invalid username or password.",
                "next_url": safe_next,
                "google_oauth_enabled": bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_OAUTH_ENABLED),
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = RedirectResponse(url=safe_next, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=_create_session_token(username.strip()),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"),
    )
    return response


@router.get("/auth/google/login")
async def google_login(request: Request, next: str = "/"):
    """Initiates Google OAuth 2.0 OpenID Connect authorization code flow."""
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_OAUTH_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google OAuth is not configured or disabled.",
        )
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    raw_state = secrets.token_urlsafe(32)
    state_payload = f"{raw_state}:{safe_next}"
    sig = hmac.new(settings.SESSION_SECRET_KEY.encode(), state_payload.encode(), hashlib.sha256).hexdigest()
    signed_state = f"{state_payload}:{sig}"

    redirect_uri = str(request.url_for("google_callback"))
    if request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https":
        redirect_uri = redirect_uri.replace("http://", "https://", 1)

    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": redirect_uri,
        "state": raw_state,
        "prompt": "select_account",
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{httpx.QueryParams(params)}"
    response = RedirectResponse(url=auth_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=signed_state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"),
    )
    return response


@router.get("/auth/google/callback")
async def google_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    """Handles Google OAuth callback, verifies allowlist, and sets session cookie."""
    if error or not code or not state:
        logger.warning("Google OAuth error or missing code/state: error=%s", error)
        return RedirectResponse(url="/login?error=oauth_failed", status_code=status.HTTP_303_SEE_OTHER)

    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if not cookie_state or cookie_state.count(":") < 2:
        logger.warning("Google OAuth callback missing or invalid state cookie")
        return RedirectResponse(url="/login?error=state_mismatch", status_code=status.HTTP_303_SEE_OTHER)

    parts = cookie_state.rsplit(":", 1)
    state_payload, sig = parts[0], parts[1]
    expected_sig = hmac.new(settings.SESSION_SECRET_KEY.encode(), state_payload.encode(), hashlib.sha256).hexdigest()
    if not secrets.compare_digest(sig, expected_sig):
        logger.warning("Google OAuth callback state signature verification failed")
        return RedirectResponse(url="/login?error=state_mismatch", status_code=status.HTTP_303_SEE_OTHER)

    raw_cookie_state, next_path = state_payload.split(":", 1)
    if not secrets.compare_digest(raw_cookie_state, state):
        logger.warning("Google OAuth callback state mismatch: param=%s vs cookie=%s", state, raw_cookie_state)
        return RedirectResponse(url="/login?error=state_mismatch", status_code=status.HTTP_303_SEE_OTHER)

    redirect_uri = str(request.url_for("google_callback"))
    if request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https":
        redirect_uri = redirect_uri.replace("http://", "https://", 1)

    token_data = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_res = await client.post(GOOGLE_TOKEN_URL, data=token_data)
            if token_res.status_code != 200:
                logger.error("Failed to exchange code for token: %s", token_res.text)
                return RedirectResponse(url="/login?error=oauth_failed", status_code=status.HTTP_303_SEE_OTHER)

            tokens = token_res.json()
            access_token = tokens.get("access_token")
            if not access_token:
                return RedirectResponse(url="/login?error=oauth_failed", status_code=status.HTTP_303_SEE_OTHER)

            userinfo_res = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_res.status_code != 200:
                logger.error("Failed to fetch Google userinfo: %s", userinfo_res.text)
                return RedirectResponse(url="/login?error=oauth_failed", status_code=status.HTTP_303_SEE_OTHER)

            userinfo = userinfo_res.json()
    except Exception as e:
        logger.error("Exception during Google OAuth callback: %s", e)
        return RedirectResponse(url="/login?error=oauth_failed", status_code=status.HTTP_303_SEE_OTHER)

    email = (userinfo.get("email") or "").strip().lower()
    name = userinfo.get("name") or email.split("@")[0]
    picture = userinfo.get("picture") or ""

    if email not in settings.allowed_google_emails_set:
        logger.warning("Unauthorized Google OAuth login attempt: %s (allowed: %s)", email, settings.allowed_google_emails_set)
        return RedirectResponse(url="/login?error=unauthorized_email", status_code=status.HTTP_303_SEE_OTHER)

    session_payload = {
        "user": email,
        "name": name,
        "avatar": picture,
    }
    token = _create_session_token(session_payload)
    dest = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
    response = RedirectResponse(url=dest, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"),
    )
    response.delete_cookie(OAUTH_STATE_COOKIE)
    return response


@router.post("/api/auth/change-password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    username: str = Depends(authenticate_user),
):
    """Allows changing the fallback password directly from the Web UI."""
    if new_password != confirm_password:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "error", "message": "New passwords do not match."},
        )
    if len(new_password) < 8:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "error", "message": "New password must be at least 8 characters long."},
        )

    valid_current = await _check_dashboard_password(current_password)
    if not valid_current:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"status": "error", "message": "Current password is incorrect."},
        )

    new_hash = _hash_password(new_password)
    await set_system_state("dashboard_password_hash", new_hash)
    logger.info("Dashboard password successfully updated for user: %s", username)

    return JSONResponse(
        content={"status": "success", "message": "Password changed successfully! Your fallback password has been updated."},
    )


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

    try:
        financing_controls = await financing_service.get_effective_risk_controls(equity)
    except Exception as e:
        logger.warning("Error fetching financing controls for dashboard: %s", e)
        financing_controls = None

    try:
        upcoming_alerts = await calendar_service.get_upcoming_alerts()
    except Exception as e:
        logger.debug("Calendar alerts lookup skipped: %s", e)
        upcoming_alerts = []

    user_info = getattr(request.state, "user", None) or {
        "user": username,
        "name": username.split("@")[0].capitalize(),
        "avatar": "",
    }

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user_info,
            "username": username,
            "upcoming_alerts": upcoming_alerts,
            "equity": equity,
            "starting_equity": starting_equity,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl_pct,
            "daily_breaker_limit": settings.DAILY_MAX_LOSS_PERCENT,
            "financing_controls": financing_controls,
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
            "telegram_configured": bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID),
            "telegram_enabled": bool(settings.TELEGRAM_ENABLED and settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID),
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

    user_info = getattr(request.state, "user", None) or {
        "user": username,
        "name": username.split("@")[0].capitalize(),
        "avatar": "",
    }

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            "user": user_info,
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


@router.get("/calendar", response_class=HTMLResponse)
async def calendar_page(
    request: Request,
    year: Optional[int] = None,
    month: Optional[int] = None,
    username: str = Depends(authenticate_user),
):
    """Interactive Market Calendar showing NYSE holidays, early closes, macro events, and basket earnings."""
    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    now_et = datetime.now(tz)
    try:
        is_market_open = await broker.is_market_open()
    except Exception:
        is_market_open = False

    cal_data = await calendar_service.get_month_view(year=year, month=month)
    user_info = getattr(request.state, "user", None) or {
        "user": username,
        "name": username.split("@")[0].capitalize(),
        "avatar": "",
    }

    return templates.TemplateResponse(
        request=request,
        name="calendar.html",
        context={
            "user": user_info,
            "username": username,
            "cal": cal_data,
            "is_market_open": is_market_open,
            "current_time_et": now_et.strftime("%H:%M:%S"),
        },
    )


@router.get("/api/calendar/events")
async def get_calendar_events_api(
    days: int = 60,
    username: str = Depends(authenticate_user),
):
    """Returns upcoming NYSE holidays, early closes, macro catalysts, earnings, and custom events."""
    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    today = datetime.now(tz).date()
    days_clamped = max(1, min(days, 365))
    events = await calendar_service.get_events_in_range(today, today + timedelta(days=days_clamped))
    return JSONResponse(content={"from": today.strftime("%Y-%m-%d"), "days": days_clamped, "events": events})


@router.post("/api/calendar/events")
async def create_calendar_event_endpoint(
    event_date: str = Form(...),
    title: str = Form(...),
    event_time_et: str = Form("ALL_DAY"),
    category: str = Form("CUSTOM"),
    impact: str = Form("MEDIUM"),
    notes: str = Form(""),
    username: str = Depends(authenticate_user),
):
    """Adds a custom trading event or reminder to the calendar."""
    await add_calendar_event(
        event_date=event_date.strip(),
        title=title.strip(),
        event_time_et=event_time_et.strip() or "ALL_DAY",
        category=category.strip() or "CUSTOM",
        impact=impact.strip() or "MEDIUM",
        notes=notes.strip() or None,
    )
    try:
        d = datetime.strptime(event_date.strip(), "%Y-%m-%d").date()
        return RedirectResponse(url=f"/calendar?year={d.year}&month={d.month}", status_code=status.HTTP_303_SEE_OTHER)
    except ValueError:
        return RedirectResponse(url="/calendar", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/calendar/events/{event_id}/delete")
async def delete_calendar_event_endpoint(
    event_id: int,
    username: str = Depends(authenticate_user),
):
    """Removes a custom calendar event."""
    await delete_calendar_event(event_id)
    return RedirectResponse(url="/calendar", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Financing, Paper-to-Live Qualification & Wealth-Building Routes
# ---------------------------------------------------------------------------


@router.get("/financing", response_class=HTMLResponse)
async def financing_page(
    request: Request,
    username: str = Depends(authenticate_user),
):
    """Renders the Capital Governance, Paper-to-Live Qualification & Wealth-Building page."""
    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    now_et = datetime.now(tz)
    current_time_et = now_et.strftime("%H:%M:%S")
    today_date = now_et.strftime("%Y-%m-%d")

    broker_equity: Optional[float] = None
    is_market_open = False
    try:
        broker_equity = await broker.get_account_equity()
        is_market_open = await broker.is_market_open()
    except Exception as e:
        logger.warning("Financing page broker query failed: %s", e)

    summary = await financing_service.build_financing_summary(broker_equity=broker_equity)
    user_info = getattr(request.state, "user", None) or {
        "user": username,
        "name": username.split("@")[0].capitalize(),
        "avatar": "",
    }

    return templates.TemplateResponse(
        request=request,
        name="financing.html",
        context={
            "user": user_info,
            "username": username,
            "current_time_et": current_time_et,
            "is_market_open": is_market_open,
            "today_date": today_date,
            "summary": summary,
        },
    )


@router.get("/api/financing/summary")
async def api_financing_summary(username: str = Depends(authenticate_user)):
    """Returns JSON payload of qualification verdict, health ladder, custody & ledger."""
    broker_equity: Optional[float] = None
    try:
        broker_equity = await broker.get_account_equity()
    except Exception:
        pass
    summary = await financing_service.build_financing_summary(broker_equity=broker_equity)
    return JSONResponse(content=summary)


@router.post("/api/financing/config")
async def api_update_financing_config(
    risk_profile: str = Form("P2_BALANCED"),
    seed_capital: float = Form(10000.0),
    target_capital_goal: float = Form(50000.0),
    reserve_pct: float = Form(20.0),
    max_live_accessible_capital: Optional[str] = Form(None),
    strategy_version: str = Form("orb_v1"),
    enforcement_enabled: Optional[str] = Form(None),
    qual_min_trades: int = Form(30),
    qual_min_trading_days: int = Form(10),
    qual_min_expectancy_r: float = Form(0.15),
    qual_min_profit_factor: float = Form(1.30),
    qual_max_drawdown_pct: float = Form(10.0),
    qual_max_avg_slippage_r: float = Form(0.10),
    username: str = Depends(authenticate_user),
):
    """Updates Financing wealth targets, risk profile, live ceiling, and qualification gates."""
    profile_key = risk_profile if risk_profile in RISK_PROFILES else "P2_BALANCED"
    cap_val: Optional[float] = None
    if max_live_accessible_capital is not None and str(max_live_accessible_capital).strip() != "":
        try:
            parsed = float(max_live_accessible_capital)
            if parsed > 0:
                cap_val = parsed
        except ValueError:
            cap_val = None

    await update_financing_config(
        {
            "risk_profile": profile_key,
            "seed_capital": max(100.0, float(seed_capital)),
            "target_capital_goal": max(float(seed_capital) + 100.0, float(target_capital_goal)),
            "reserve_pct": max(0.0, min(90.0, float(reserve_pct))),
            "max_live_accessible_capital": cap_val,
            "strategy_version": (strategy_version or "orb_v1").strip()[:40],
            "enforcement_enabled": 1 if enforcement_enabled else 0,
            "qual_min_trades": max(5, int(qual_min_trades)),
            "qual_min_trading_days": max(1, int(qual_min_trading_days)),
            "qual_min_expectancy_r": float(qual_min_expectancy_r),
            "qual_min_profit_factor": max(1.0, float(qual_min_profit_factor)),
            "qual_max_drawdown_pct": max(1.0, float(qual_max_drawdown_pct)),
            "qual_max_avg_slippage_r": max(0.01, float(qual_max_avg_slippage_r)),
        }
    )
    return RedirectResponse(url="/financing", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/financing/stage")
async def api_update_financing_stage(
    stage: str = Form(...),
    username: str = Depends(authenticate_user),
):
    """Transitions strategy lifecycle stage (Paper Qualification -> Live Pilot -> Live Active / Suspended / Rejected)."""
    clean_stage = stage.strip().upper()
    if clean_stage not in VALID_LIFECYCLE_STAGES:
        raise HTTPException(status_code=400, detail=f"Invalid lifecycle stage: {stage}")
    await update_financing_config({"lifecycle_stage": clean_stage})
    logger.warning("Financing lifecycle stage updated to %s by %s", clean_stage, username)
    return RedirectResponse(url="/financing", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/financing/checks/{check_id}")
async def api_update_validation_check(
    check_id: str,
    status_val: str = Form(..., alias="status"),
    notes: str = Form(""),
    username: str = Depends(authenticate_user),
):
    """Updates an interactive operational safety sign-off check."""
    clean_status = status_val.strip().upper()
    if clean_status not in ("NOT_RUN", "PASSED", "FAILED"):
        raise HTTPException(status_code=400, detail="Invalid check status")
    await set_validation_check(check_id.strip().upper(), clean_status, notes)
    return RedirectResponse(url="/financing", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/financing/ledger")
async def api_add_capital_ledger_entry(
    flow_type: str = Form(...),
    amount: float = Form(...),
    event_date: Optional[str] = Form(None),
    notes: str = Form(""),
    username: str = Depends(authenticate_user),
):
    """Logs a capital movement (deposit, withdrawal, vault sweep, vault funding) separate from trading P&L."""
    clean_type = flow_type.strip().upper()
    if clean_type not in VALID_LEDGER_FLOW_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid flow_type: {flow_type}")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    date_str = (event_date or "").strip() or datetime.now(ZoneInfo(settings.MARKET_TIMEZONE)).strftime("%Y-%m-%d")
    direction = FLOW_DIRECTIONS.get(clean_type, "EXTERNAL_IN")
    await add_capital_ledger_entry(
        event_date=date_str,
        flow_type=clean_type,
        amount=round(float(amount), 2),
        direction=direction,
        notes=notes.strip(),
    )
    return RedirectResponse(url="/financing", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/financing/brake/latch")
async def api_latch_dead_mans_brake(
    reason: str = Form("Manual operator safety latch"),
    username: str = Depends(authenticate_user),
):
    """Manually latches the Dead Man's Brake (for operational testing or emergency lock)."""
    await financing_service.latch_dead_mans_brake(f"{reason} (by {username})")
    return RedirectResponse(url="/financing", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/financing/brake/reset")
async def api_reset_dead_mans_brake(username: str = Depends(authenticate_user)):
    """Explicitly resets the Dead Man's Brake after operator reconciliation."""
    await financing_service.reset_dead_mans_brake()
    return RedirectResponse(url="/financing", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/financing/health/reset")
async def api_reset_health_latch(username: str = Depends(authenticate_user)):
    """Explicitly resets a latched SUSPENDED/CAPITAL_PROTECTION health state after review."""
    await financing_service.reset_health_latch()
    return RedirectResponse(url="/financing", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/api/financing/tax-pnl.csv")
async def api_export_tax_pnl_csv(username: str = Depends(authenticate_user)):
    """Downloads realised trading P&L CSV (excluding deposits, withdrawals, and vault sweeps)."""
    csv_text = await financing_service.build_tax_pnl_csv()
    today_str = datetime.now(ZoneInfo(settings.MARKET_TIMEZONE)).strftime("%Y-%m-%d")
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="ptrading_realised_pnl_{today_str}.csv"'},
    )


@router.post("/api/telegram/test")
async def test_telegram_connection(username: str = Depends(authenticate_user)):
    """Sends an instant test ping to Telegram to verify credentials and chat connection."""
    token = (settings.TELEGRAM_BOT_TOKEN or "").strip().strip('"').strip("'")
    if token.lower().startswith("bot"):
        token = token[3:]
    chat_id = (settings.TELEGRAM_CHAT_ID or "").strip().strip('"').strip("'")

    if not token or not chat_id:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "message": "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env before testing.",
            },
        )

    if ":" not in token or len(token) < 20:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "message": "Invalid TELEGRAM_BOT_TOKEN format. The bot token from @BotFather must have numbers, a colon, and letters (e.g. 123456789:ABCdef...).",
            },
        )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"🔔 <b>pTrading Alert Test</b>\n"
        f"<b>Status:</b> ✅ Connected Successfully\n"
        f"<b>Environment:</b> <code>{settings.ENVIRONMENT}</code>\n"
        f"<b>Initiated by:</b> <code>{username}</code>\n"
        f"<b>Time:</b> {now_str}\n\n"
        f"<i>Your pTrading bot is online and will deliver live signals and reports to this chat!</i>"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
            data = res.json()
            if res.status_code == 200 and data.get("ok"):
                return JSONResponse(
                    content={
                        "status": "success",
                        "message": "Test alert delivered to your Telegram app!",
                    }
                )
            err_desc = data.get("description", f"HTTP {res.status_code}")
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={"status": "error", "message": f"Telegram API error: {err_desc}"},
            )
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"status": "error", "message": f"Network error connecting to Telegram: {str(e)}"},
        )


@router.post("/api/telegram/set-webhook")
async def set_telegram_webhook(username: str = Depends(authenticate_user)):
    """Registers the Telegram webhook so inline Approve/Reject buttons work from your phone."""
    if not telegram.token:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "error", "message": "TELEGRAM_BOT_TOKEN is not set in .env."},
        )
    domain = settings.DOMAIN_NAME or "trading2564.com"
    webhook_url = f"https://{domain}/webhook/telegram"
    ok, msg = await telegram.set_webhook(webhook_url)
    if ok:
        return JSONResponse(content={"status": "success", "message": f"Telegram webhook registered: {msg}"})
    return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content={"status": "error", "message": msg})


@router.get("/api/telegram/status")
async def get_telegram_status(username: str = Depends(authenticate_user)):
    """Returns Telegram configuration and connectivity status."""
    token = telegram.token
    chat_id = telegram.chat_id
    configured = telegram.is_configured
    masked_chat = f"{chat_id[:3]}...{chat_id[-2:]}" if len(chat_id) > 5 else (chat_id if chat_id else "Not set")
    return {
        "enabled": settings.TELEGRAM_ENABLED,
        "configured": configured,
        "chat_id": masked_chat,
    }




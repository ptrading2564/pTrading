"""Isolated Backtest Web UI & API Router (/backtest and /api/backtest/*).

Strictly isolated from live trading:
- Reuses existing dashboard auth & Jinja2 templates read-only.
- Persists only to dedicated data/backtest.db and data/backtest_cache.db.
- Never writes to live data/ptrading.db or submits broker orders.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.routers.dashboard import authenticate_user, templates
from app.services.market_scanner import MarketScanner
from backtest.config import BacktestConfig, resolve_backtest_alpaca_credentials
from backtest.data_loader import AlpacaCachedDataStore
from backtest.db import (
    create_backtest_run,
    delete_backtest_run,
    get_backtest_run,
    init_backtest_db,
    list_backtest_runs,
)
from backtest.engine import BacktestEngine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Backtest"])

# Active background tasks: {run_id: (asyncio.Task, cancel_event)}
_ACTIVE_TASKS: Dict[str, Tuple[asyncio.Task, asyncio.Event]] = {}


class BacktestRunRequest(BaseModel):
    name: str = ""
    mode: str = Field(default="screener", pattern="^(screener|fixed|chartmill_history)$")
    start_date: str
    end_date: str
    feed: str = Field(default="sip", pattern="^(sip|iex)$")
    initial_capital: float = Field(default=50000.0, gt=0)
    risk_profile: str = Field(default="P2_BALANCED", pattern="^(P1_DEFENSIVE|P2_BALANCED|P3_GROWTH|CUSTOM)$")
    risk_percent_per_trade: float = Field(default=0.5, gt=0, le=5.0)
    max_basket_size: Optional[int] = Field(default=38, ge=0, le=500)
    custom_symbols: str = ""
    scanner_min_rvol: float = Field(default=1.5, ge=0.5, le=10.0)
    scanner_max_atr_extension: float = Field(default=0.75, ge=0.1, le=5.0)
    breakeven_trigger_r: float = Field(default=1.0, ge=0.25, le=5.0)
    take_profit_r: float = Field(default=2.0, ge=0.5, le=10.0)
    exit_slippage_bps: float = Field(default=3.0, ge=0.0, le=100.0)
    enable_slot_recycling: bool = True
    enable_health_ladder: bool = True
    enable_fomc_guard: bool = True
    enable_early_close_guard: bool = True
    enable_spy_qqq_gate: bool = True
    lock_triggered_on_reject: bool = True
    use_1m_execution: bool = True
    enable_edgar_fundamentals: bool = False
    screener_min_fundamental_rating: float = Field(default=5.0, ge=0.0, le=10.0)
    run_gemini_review: bool = False


async def _run_backtest_background(run_id: str, cfg: BacktestConfig, cancel_event: asyncio.Event) -> None:
    try:
        engine = BacktestEngine(cfg)
        await engine.run(run_id=run_id, persist=True, cancel_event=cancel_event)
    except asyncio.CancelledError:
        logger.info("Background backtest %s cancelled.", run_id)
    except Exception as e:
        logger.error("Background backtest %s failed: %s", run_id, e)
    finally:
        _ACTIVE_TASKS.pop(run_id, None)


@router.get("/backtest", response_class=HTMLResponse)
async def backtest_page(
    request: Request,
    run_id: Optional[str] = None,
    compare_id: Optional[str] = None,
    username: str = Depends(authenticate_user),
):
    """Main interactive Backtest Lab page accessible from top dashboard navigation."""
    await init_backtest_db()
    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    now_et = datetime.now(tz)

    runs = await list_backtest_runs(limit=30)

    selected_run: Optional[Dict[str, Any]] = None
    if run_id:
        selected_run = await get_backtest_run(run_id)
    elif runs:
        # Prefer the latest completed run, else latest run
        latest_done = next((r for r in runs if r["status"] == "COMPLETED"), runs[0])
        selected_run = await get_backtest_run(latest_done["id"])

    compare_run: Optional[Dict[str, Any]] = None
    if compare_id and (not selected_run or compare_id != selected_run["id"]):
        compare_run = await get_backtest_run(compare_id)

    _, _, cred_source = resolve_backtest_alpaca_credentials()
    try:
        cache_stats = AlpacaCachedDataStore().get_cache_stats()
    except Exception:
        cache_stats = {"distinct_symbols": 0}

    # Default date window: trailing 6 months ending yesterday (safe for SIP historical)
    default_end = (now_et.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    default_start = (now_et.date() - timedelta(days=180)).strftime("%Y-%m-%d")

    return templates.TemplateResponse(
        request=request,
        name="backtest.html",
        context={
            "username": username,
            "user": getattr(request.state, "user", None),
            "is_market_open": False,
            "current_time_et": now_et.strftime("%H:%M:%S"),
            "runs": runs,
            "selected_run": selected_run,
            "compare_run": compare_run,
            "cred_source": cred_source,
            "cache_stats": cache_stats,
            "default_start": default_start,
            "default_end": default_end,
            "defaults": {
                "risk_pct": settings.RISK_PERCENT_PER_TRADE,
                "min_rvol": settings.SCANNER_MIN_RVOL,
                "max_atr_ext": settings.SCANNER_MAX_ATR_EXTENSION,
                "be_trigger_r": settings.BREAKEVEN_TRIGGER_R,
                "max_positions": settings.MAX_CONCURRENT_POSITIONS,
                "max_total_be": settings.MAX_TOTAL_POSITIONS_WITH_BREAKEVEN,
                "gemini_configured": bool(settings.GEMINI_API_KEY),
            },
        },
    )


@router.post("/api/backtest/run")
async def start_backtest_run(
    payload: BacktestRunRequest,
    username: str = Depends(authenticate_user),
):
    """Launches a non-blocking background backtest job and returns its run_id."""
    try:
        s_d = date.fromisoformat(payload.start_date)
        e_d = date.fromisoformat(payload.end_date)
        if e_d < s_d:
            raise ValueError("End date must be on or after start date.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    api_key, secret_key, _ = resolve_backtest_alpaca_credentials()
    if not api_key or not secret_key:
        raise HTTPException(
            status_code=400,
            detail="Alpaca API credentials are not configured. Add BACKTEST_ALPACA_API_KEY and BACKTEST_ALPACA_SECRET_KEY to .env.",
        )

    custom_syms = [
        s.strip().upper()
        for s in payload.custom_symbols.split(",")
        if s.strip()
    ]
    basket_cap = payload.max_basket_size if (payload.max_basket_size and payload.max_basket_size > 0) else None
    run_name = payload.name.strip() or (
        f"{payload.mode.upper()} ({payload.feed.upper()}) {payload.start_date}..{payload.end_date}"
    )

    cfg = BacktestConfig(
        name=run_name,
        mode=payload.mode,
        start_date=payload.start_date,
        end_date=payload.end_date,
        feed=payload.feed,
        initial_capital=payload.initial_capital,
        risk_profile=payload.risk_profile,
        risk_percent_per_trade=payload.risk_percent_per_trade,
        max_basket_size=basket_cap,
        custom_symbols=custom_syms,
        scanner_min_rvol=payload.scanner_min_rvol,
        scanner_max_atr_extension=payload.scanner_max_atr_extension,
        breakeven_trigger_r=payload.breakeven_trigger_r,
        take_profit_r=payload.take_profit_r,
        exit_slippage_bps=payload.exit_slippage_bps,
        enable_slot_recycling=payload.enable_slot_recycling,
        enable_health_ladder=payload.enable_health_ladder,
        enable_fomc_guard=payload.enable_fomc_guard,
        enable_early_close_guard=payload.enable_early_close_guard,
        enable_spy_qqq_gate=payload.enable_spy_qqq_gate,
        lock_triggered_on_reject=payload.lock_triggered_on_reject,
        use_1m_execution=payload.use_1m_execution,
        enable_edgar_fundamentals=payload.enable_edgar_fundamentals,
        screener_min_fundamental_rating=payload.screener_min_fundamental_rating,
        run_gemini_review=payload.run_gemini_review,
    )

    run_id = await create_backtest_run(
        name=run_name,
        mode=cfg.mode,
        feed=cfg.feed,
        start_date=cfg.start_date,
        end_date=cfg.end_date,
        config_dict=cfg.to_dict(),
    )

    cancel_ev = asyncio.Event()
    task = asyncio.create_task(_run_backtest_background(run_id, cfg, cancel_ev))
    _ACTIVE_TASKS[run_id] = (task, cancel_ev)
    logger.info("User %s started backtest run %s (%s)", username, run_id, run_name)

    return JSONResponse(content={"run_id": run_id, "status": "QUEUED"})


@router.get("/api/backtest/status/{run_id}")
async def get_backtest_status(
    run_id: str,
    username: str = Depends(authenticate_user),
):
    """Returns live progress status for an active or completed backtest run."""
    run = await get_backtest_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return JSONResponse(
        content={
            "run_id": run["id"],
            "status": run["status"],
            "progress_pct": run.get("progress_pct", 0.0),
            "progress_msg": run.get("progress_msg", ""),
            "error_message": run.get("error_message"),
        }
    )


@router.post("/api/backtest/cancel/{run_id}")
async def cancel_backtest_run(
    run_id: str,
    username: str = Depends(authenticate_user),
):
    """Signals cancellation for a running background backtest task."""
    entry = _ACTIVE_TASKS.get(run_id)
    if entry:
        _, cancel_ev = entry
        cancel_ev.set()
        return JSONResponse(content={"run_id": run_id, "cancelled": True})
    return JSONResponse(content={"run_id": run_id, "cancelled": False})


@router.delete("/api/backtest/runs/{run_id}")
async def remove_backtest_run(
    run_id: str,
    username: str = Depends(authenticate_user),
):
    entry = _ACTIVE_TASKS.get(run_id)
    if entry:
        entry[1].set()
    deleted = await delete_backtest_run(run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return JSONResponse(content={"deleted": True, "run_id": run_id})


@router.get("/api/backtest/runs/{run_id}/report.json")
async def export_backtest_json(
    run_id: str,
    username: str = Depends(authenticate_user),
):
    run = await get_backtest_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return JSONResponse(
        content={
            "run_id": run["id"],
            "name": run["name"],
            "config": run["config"],
            "summary": run["summary"],
            "report": run["report"],
            "diagnostics": run["diagnostics"],
        }
    )


@router.get("/api/backtest/runs/{run_id}/trades.csv")
async def export_backtest_trades_csv(
    run_id: str,
    username: str = Depends(authenticate_user),
):
    run = await get_backtest_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    output = io.StringIO()
    fields = [
        "trade_date", "symbol", "qty", "signal_time", "entry_time", "exit_time",
        "signal_price", "entry_price", "initial_stop_price", "take_profit_price",
        "exit_price", "pnl_dollars", "pnl_r", "exit_reason", "breakeven_stepped",
        "stop_type", "mfe_r", "mae_r", "post_exit_max_r", "signal_rvol",
        "cm_tech_rating", "cm_setup_rating", "cm_rs", "cm_fundamental_rating",
        "health_state", "risk_multiplier",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for t in run.get("trades", []):
        writer.writerow(t)

    return PlainTextResponse(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="ptrading_backtest_{run_id}_trades.csv"'},
    )


@router.get("/api/backtest/runs/{run_id}/chart/{trade_id}")
async def get_backtest_trade_chart(
    run_id: str,
    trade_id: int,
    username: str = Depends(authenticate_user),
):
    """Returns 5m candlesticks + VWAP + OR High/Low + simulated trade overlay for the Lightweight Charts modal."""
    run = await get_backtest_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    trade = next((t for t in run.get("trades", []) if int(t["id"]) == trade_id), None)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found in backtest run")

    sym = str(trade["symbol"]).upper()
    t_date_str = str(trade["trade_date"])
    t_day = date.fromisoformat(t_date_str)
    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    feed = str(run.get("feed") or "sip").lower()

    session_start = datetime.combine(t_day, time(9, 30), tzinfo=tz)
    session_end = datetime.combine(t_day, time(16, 0), tzinfo=tz)

    store = AlpacaCachedDataStore()
    bars_map = await store.get_bars([sym], "5Min", session_start, session_end, feed=feed)
    bars = bars_map.get(sym, [])

    candles = []
    vwap_points = []
    running_bars = []
    for b in bars:
        dt_et = b.timestamp.astimezone(tz)
        # Shift timestamp to ET wall-clock epoch for Lightweight Charts display (matches dashboard.py)
        offset_sec = dt_et.utcoffset().total_seconds() if dt_et.utcoffset() else 0
        et_wall = int(b.timestamp.timestamp() + offset_sec)
        candles.append(
            {
                "time": et_wall,
                "open": round(b.open, 2),
                "high": round(b.high, 2),
                "low": round(b.low, 2),
                "close": round(b.close, 2),
            }
        )
        running_bars.append(b)
        vwap_val = MarketScanner.calculate_intraday_vwap(running_bars)
        vwap_points.append({"time": et_wall, "value": round(vwap_val, 2)})

    return JSONResponse(
        content={
            "symbol": sym,
            "date": t_date_str,
            "candles": candles,
            "vwap": vwap_points,
            "or_high": trade.get("or_high"),
            "or_low": trade.get("or_low"),
            "trade": trade,
        }
    )

"""Dedicated async SQLite persistence for backtest runs, trades, and daily equity curves.

Strictly isolated in data/backtest.db so backtests never write to or lock live data/ptrading.db.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional
import aiosqlite

from backtest.config import get_backtest_db_path


@asynccontextmanager
async def get_backtest_connection() -> AsyncGenerator[aiosqlite.Connection, None]:
    db_path = get_backtest_db_path()
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode = WAL;")
    await conn.execute("PRAGMA synchronous = NORMAL;")
    await conn.execute("PRAGMA busy_timeout = 5000;")
    await conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
    finally:
        await conn.close()


async def init_backtest_db() -> None:
    """Initializes isolated backtest tables in data/backtest.db."""
    async with get_backtest_connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'QUEUED',
                progress_pct REAL DEFAULT 0.0,
                progress_msg TEXT DEFAULT '',
                mode TEXT NOT NULL,
                feed TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                config_json TEXT NOT NULL,
                summary_json TEXT,
                report_json TEXT,
                review_markdown TEXT,
                review_source TEXT DEFAULT 'deterministic',
                diagnostics_json TEXT,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backtest_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                qty INTEGER NOT NULL,
                signal_time TEXT,
                entry_time TEXT,
                exit_time TEXT,
                signal_price REAL,
                entry_limit_price REAL,
                entry_price REAL,
                initial_stop_price REAL,
                stop_loss_price REAL,
                take_profit_price REAL,
                exit_price REAL,
                initial_risk_dollars REAL,
                pnl_dollars REAL,
                pnl_r REAL,
                slippage_dollars REAL,
                slippage_r REAL,
                stop_type TEXT,
                exit_reason TEXT,
                breakeven_stepped INTEGER DEFAULT 0,
                mfe_r REAL,
                mae_r REAL,
                post_exit_max_r REAL,
                signal_rvol REAL,
                signal_atr_ext REAL,
                or_high REAL,
                or_low REAL,
                or_width_pct REAL,
                cm_tech_rating REAL,
                cm_setup_rating REAL,
                cm_rs REAL,
                cm_fundamental_rating REAL,
                health_state TEXT,
                risk_multiplier REAL,
                FOREIGN KEY (run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backtest_daily_equity (
                run_id TEXT NOT NULL,
                date TEXT NOT NULL,
                starting_equity REAL NOT NULL,
                ending_equity REAL NOT NULL,
                daily_pnl REAL NOT NULL,
                daily_roi_pct REAL NOT NULL,
                drawdown_pct REAL NOT NULL,
                health_state TEXT,
                trade_count INTEGER DEFAULT 0,
                circuit_breaker_tripped INTEGER DEFAULT 0,
                spy_return_pct REAL,
                qqq_return_pct REAL,
                market_regime TEXT,
                basket_size INTEGER DEFAULT 0,
                PRIMARY KEY (run_id, date),
                FOREIGN KEY (run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
            );
            """
        )

        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON backtest_trades(run_id, trade_date);"
        )
        await conn.commit()


async def create_backtest_run(
    name: str,
    mode: str,
    feed: str,
    start_date: str,
    end_date: str,
    config_dict: Dict[str, Any],
    run_id: Optional[str] = None,
) -> str:
    """Creates a new QUEUED backtest run record and returns its run_id."""
    await init_backtest_db()
    rid = run_id or uuid.uuid4().hex[:12]
    async with get_backtest_connection() as conn:
        await conn.execute(
            """
            INSERT INTO backtest_runs (id, name, status, progress_pct, progress_msg, mode, feed, start_date, end_date, config_json)
            VALUES (?, ?, 'QUEUED', 0.0, 'Queued...', ?, ?, ?, ?, ?);
            """,
            (rid, name, mode, feed, start_date, end_date, json.dumps(config_dict)),
        )
        await conn.commit()
    return rid


async def update_backtest_progress(
    run_id: str,
    status: str,
    progress_pct: float,
    progress_msg: str,
) -> None:
    async with get_backtest_connection() as conn:
        await conn.execute(
            """
            UPDATE backtest_runs
            SET status = ?, progress_pct = ?, progress_msg = ?
            WHERE id = ?;
            """,
            (status, round(max(0.0, min(100.0, progress_pct)), 1), progress_msg, run_id),
        )
        await conn.commit()


async def fail_backtest_run(run_id: str, error_message: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_backtest_connection() as conn:
        await conn.execute(
            """
            UPDATE backtest_runs
            SET status = 'FAILED', progress_msg = 'Failed', error_message = ?, completed_at = ?
            WHERE id = ?;
            """,
            (error_message[:2000], now_iso, run_id),
        )
        await conn.commit()


async def save_backtest_result(
    run_id: str,
    summary: Dict[str, Any],
    report: Dict[str, Any],
    trades: List[Dict[str, Any]],
    daily_rows: List[Dict[str, Any]],
    review_markdown: str,
    review_source: str,
    diagnostics: List[Dict[str, Any]],
) -> None:
    """Persists completed backtest summary, full report, daily curve, and trade log atomically."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_backtest_connection() as conn:
        await conn.execute("DELETE FROM backtest_trades WHERE run_id = ?;", (run_id,))
        await conn.execute("DELETE FROM backtest_daily_equity WHERE run_id = ?;", (run_id,))

        if trades:
            await conn.executemany(
                """
                INSERT INTO backtest_trades (
                    run_id, trade_date, symbol, qty, signal_time, entry_time, exit_time,
                    signal_price, entry_limit_price, entry_price, initial_stop_price,
                    stop_loss_price, take_profit_price, exit_price, initial_risk_dollars,
                    pnl_dollars, pnl_r, slippage_dollars, slippage_r, stop_type, exit_reason,
                    breakeven_stepped, mfe_r, mae_r, post_exit_max_r, signal_rvol,
                    signal_atr_ext, or_high, or_low, or_width_pct, cm_tech_rating,
                    cm_setup_rating, cm_rs, cm_fundamental_rating, health_state, risk_multiplier
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                );
                """,
                [
                    (
                        run_id,
                        t.get("trade_date"),
                        t.get("symbol"),
                        int(t.get("qty") or 0),
                        t.get("signal_time"),
                        t.get("entry_time"),
                        t.get("exit_time"),
                        t.get("signal_price"),
                        t.get("entry_limit_price"),
                        t.get("entry_price"),
                        t.get("initial_stop_price"),
                        t.get("stop_loss_price"),
                        t.get("take_profit_price"),
                        t.get("exit_price"),
                        t.get("initial_risk_dollars"),
                        t.get("pnl_dollars"),
                        t.get("pnl_r"),
                        t.get("slippage_dollars"),
                        t.get("slippage_r"),
                        t.get("stop_type"),
                        t.get("exit_reason"),
                        1 if t.get("breakeven_stepped") else 0,
                        t.get("mfe_r"),
                        t.get("mae_r"),
                        t.get("post_exit_max_r"),
                        t.get("signal_rvol"),
                        t.get("signal_atr_ext"),
                        t.get("or_high"),
                        t.get("or_low"),
                        t.get("or_width_pct"),
                        t.get("cm_tech_rating"),
                        t.get("cm_setup_rating"),
                        t.get("cm_rs"),
                        t.get("cm_fundamental_rating"),
                        t.get("health_state"),
                        t.get("risk_multiplier"),
                    )
                    for t in trades
                ],
            )

        if daily_rows:
            await conn.executemany(
                """
                INSERT INTO backtest_daily_equity (
                    run_id, date, starting_equity, ending_equity, daily_pnl, daily_roi_pct,
                    drawdown_pct, health_state, trade_count, circuit_breaker_tripped,
                    spy_return_pct, qqq_return_pct, market_regime, basket_size
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                [
                    (
                        run_id,
                        d["date"],
                        d["starting_equity"],
                        d["ending_equity"],
                        d["daily_pnl"],
                        d["daily_roi_pct"],
                        d["drawdown_pct"],
                        d.get("health_state", "HEALTHY"),
                        int(d.get("trade_count", 0)),
                        1 if d.get("circuit_breaker_tripped") else 0,
                        d.get("spy_return_pct"),
                        d.get("qqq_return_pct"),
                        d.get("market_regime"),
                        int(d.get("basket_size", 0)),
                    )
                    for d in daily_rows
                ],
            )

        await conn.execute(
            """
            UPDATE backtest_runs
            SET status = 'COMPLETED',
                progress_pct = 100.0,
                progress_msg = 'Completed',
                summary_json = ?,
                report_json = ?,
                review_markdown = ?,
                review_source = ?,
                diagnostics_json = ?,
                completed_at = ?
            WHERE id = ?;
            """,
            (
                json.dumps(summary),
                json.dumps(report),
                review_markdown,
                review_source,
                json.dumps(diagnostics),
                now_iso,
                run_id,
            ),
        )
        await conn.commit()


async def list_backtest_runs(limit: int = 30) -> List[Dict[str, Any]]:
    await init_backtest_db()
    async with get_backtest_connection() as conn:
        rows = await (
            await conn.execute(
                """
                SELECT id, name, status, progress_pct, progress_msg, mode, feed,
                       start_date, end_date, summary_json, error_message, created_at, completed_at
                FROM backtest_runs
                ORDER BY created_at DESC
                LIMIT ?;
                """,
                (limit,),
            )
        ).fetchall()
    results = []
    for r in rows:
        item = dict(r)
        item["summary"] = json.loads(item["summary_json"]) if item.get("summary_json") else {}
        results.append(item)
    return results


async def get_backtest_run(run_id: str) -> Optional[Dict[str, Any]]:
    await init_backtest_db()
    async with get_backtest_connection() as conn:
        row = await (
            await conn.execute("SELECT * FROM backtest_runs WHERE id = ?;", (run_id,))
        ).fetchone()
        if not row:
            return None
        run = dict(row)
        run["config"] = json.loads(run["config_json"]) if run.get("config_json") else {}
        run["summary"] = json.loads(run["summary_json"]) if run.get("summary_json") else {}
        run["report"] = json.loads(run["report_json"]) if run.get("report_json") else {}
        run["diagnostics"] = json.loads(run["diagnostics_json"]) if run.get("diagnostics_json") else []

        trades_rows = await (
            await conn.execute(
                "SELECT * FROM backtest_trades WHERE run_id = ? ORDER BY trade_date ASC, entry_time ASC, id ASC;",
                (run_id,),
            )
        ).fetchall()
        daily_rows = await (
            await conn.execute(
                "SELECT * FROM backtest_daily_equity WHERE run_id = ? ORDER BY date ASC;",
                (run_id,),
            )
        ).fetchall()
        run["trades"] = [dict(t) for t in trades_rows]
        run["daily_equity"] = [dict(d) for d in daily_rows]
        return run


async def delete_backtest_run(run_id: str) -> bool:
    await init_backtest_db()
    async with get_backtest_connection() as conn:
        cur = await conn.execute("DELETE FROM backtest_runs WHERE id = ?;", (run_id,))
        await conn.commit()
        return cur.rowcount > 0

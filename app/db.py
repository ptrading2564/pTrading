"""Async SQLite database manager with WAL mode enabled."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
import aiosqlite

from app.config import settings


def get_db_path() -> Path:
    db_path = settings.DATABASE_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


@asynccontextmanager
async def get_connection() -> AsyncGenerator[aiosqlite.Connection, None]:
    db_path = get_db_path()
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


async def init_db() -> None:
    """Initializes tables and default system states."""
    async with get_connection() as conn:
        # Signals table with unique constraint on (bar_time, symbol) for idempotency
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                bar_time INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                close_price REAL NOT NULL,
                or_high REAL NOT NULL,
                or_low REAL NOT NULL,
                atr REAL NOT NULL,
                status TEXT NOT NULL,
                rejection_reason TEXT,
                raw_payload TEXT,
                UNIQUE(bar_time, symbol)
            );
            """
        )

        # Orders table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_order_id TEXT UNIQUE NOT NULL,
                broker_order_id TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty INTEGER NOT NULL,
                entry_limit_price REAL NOT NULL,
                stop_loss_price REAL NOT NULL,
                take_profit_price REAL NOT NULL,
                stop_loss_order_id TEXT,
                take_profit_order_id TEXT,
                status TEXT NOT NULL,
                filled_avg_price REAL,
                filled_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Trades table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_order_id TEXT UNIQUE NOT NULL,
                symbol TEXT NOT NULL,
                qty INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                entry_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                stop_loss_price REAL NOT NULL,
                initial_stop_price REAL,
                initial_risk_dollars REAL,
                take_profit_price REAL NOT NULL,
                signal_price REAL,
                slippage_dollars REAL,
                slippage_r REAL,
                stop_type TEXT,
                signal_rvol REAL,
                signal_atr_ext REAL,
                or_width_pct REAL,
                mfe_r REAL,
                mae_r REAL,
                post_exit_max_r REAL,
                trade_date TEXT,
                exit_price REAL,
                exit_time TIMESTAMP,
                pnl_dollars REAL,
                pnl_r REAL,
                exit_reason TEXT,
                breakeven_stepped INTEGER DEFAULT 0,
                is_open INTEGER DEFAULT 1
            );
            """
        )

        # Daily stats table for tracking equity and circuit breaker
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                starting_equity REAL NOT NULL,
                current_equity REAL NOT NULL,
                realized_pnl REAL DEFAULT 0.0,
                unrealized_pnl REAL DEFAULT 0.0,
                trade_count INTEGER DEFAULT 0,
                circuit_breaker_tripped INTEGER DEFAULT 0,
                spy_return_pct REAL,
                qqq_return_pct REAL,
                market_regime TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Daily comprehensive reports table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_reports (
                date TEXT PRIMARY KEY,
                metrics_json TEXT NOT NULL,
                review_markdown TEXT NOT NULL,
                review_source TEXT NOT NULL DEFAULT 'deterministic',
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Idempotent migrations for existing databases
        await _ensure_columns(
            conn,
            "trades",
            {
                "initial_stop_price": "REAL",
                "initial_risk_dollars": "REAL",
                "signal_price": "REAL",
                "slippage_dollars": "REAL",
                "slippage_r": "REAL",
                "stop_type": "TEXT",
                "signal_rvol": "REAL",
                "signal_atr_ext": "REAL",
                "or_width_pct": "REAL",
                "mfe_r": "REAL",
                "mae_r": "REAL",
                "post_exit_max_r": "REAL",
                "trade_date": "TEXT",
            },
        )
        await _ensure_columns(
            conn,
            "daily_stats",
            {
                "spy_return_pct": "REAL",
                "qqq_return_pct": "REAL",
                "market_regime": "TEXT",
            },
        )

        # System state key-value table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Default system mode if not present
        await conn.execute(
            """
            INSERT OR IGNORE INTO system_state (key, value)
            VALUES ('execution_mode', ?);
            """,
            (settings.EXECUTION_MODE.value,),
        )
        await conn.execute(
            """
            INSERT OR IGNORE INTO system_state (key, value)
            VALUES ('kill_switch', 'OFF');
            """,
        )
        await conn.commit()


async def _ensure_columns(
    conn: aiosqlite.Connection, table: str, columns: Dict[str, str]
) -> None:
    """Adds missing columns to an existing SQLite table idempotently."""
    cursor = await conn.execute(f"PRAGMA table_info({table});")
    existing = {row["name"] for row in await cursor.fetchall()}
    for col_name, col_type in columns.items():
        if col_name not in existing:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type};")


# Helper Functions for DB Queries


async def get_system_state(key: str, default: Optional[str] = None) -> Optional[str]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT value FROM system_state WHERE key = ?;", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else default


async def set_system_state(key: str, value: str) -> None:
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
            """,
            (key, value),
        )
        await conn.commit()


async def record_signal(
    bar_time: int,
    symbol: str,
    close_price: float,
    or_high: float,
    or_low: float,
    atr: float,
    status: str,
    rejection_reason: Optional[str] = None,
    raw_payload: Optional[str] = None,
) -> Optional[int]:
    """Records an incoming signal. Returns signal id or None if duplicate."""
    async with get_connection() as conn:
        try:
            cursor = await conn.execute(
                """
                INSERT INTO signals (bar_time, symbol, close_price, or_high, or_low, atr, status, rejection_reason, raw_payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    bar_time,
                    symbol,
                    close_price,
                    or_high,
                    or_low,
                    atr,
                    status,
                    rejection_reason,
                    raw_payload,
                ),
            )
            await conn.commit()
            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            # Duplicate signal on bar_time + symbol
            return None


async def get_recent_signals(limit: int = 50) -> List[Dict[str, Any]]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?;", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_open_trades() -> List[Dict[str, Any]]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM trades WHERE is_open = 1 ORDER BY id ASC;"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def save_daily_report(
    report_date: str,
    metrics: Dict[str, Any],
    review_markdown: str,
    review_source: str = "deterministic",
) -> None:
    """Persists or updates a daily comprehensive analysis report."""
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO daily_reports (date, metrics_json, review_markdown, review_source, generated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(date) DO UPDATE SET
                metrics_json = excluded.metrics_json,
                review_markdown = excluded.review_markdown,
                review_source = excluded.review_source,
                generated_at = CURRENT_TIMESTAMP;
            """,
            (report_date, json.dumps(metrics), review_markdown, review_source),
        )
        await conn.commit()


async def get_daily_report(report_date: str) -> Optional[Dict[str, Any]]:
    """Fetches a stored daily report by YYYY-MM-DD date."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM daily_reports WHERE date = ?;", (report_date,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["metrics"] = json.loads(data["metrics_json"])
        except Exception:
            data["metrics"] = {}
        return data


async def list_daily_report_dates(limit: int = 60) -> List[str]:
    """Returns stored report dates in descending order."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT date FROM daily_reports ORDER BY date DESC LIMIT ?;", (limit,)
        )
        return [row["date"] for row in await cursor.fetchall()]

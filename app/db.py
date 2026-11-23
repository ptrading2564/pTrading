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
                take_profit_price REAL NOT NULL,
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
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
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

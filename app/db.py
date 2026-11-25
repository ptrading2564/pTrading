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

        # ChartMill daily per-symbol technical & fundamental ratings table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chartmill_universe (
                date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                company_name TEXT,
                price REAL,
                atr_pct REAL,
                setup_rating REAL,
                tech_rating REAL,
                relative_strength REAL,
                composite_score REAL,
                fundamental_rating REAL,
                growth_rating REAL,
                health_rating REAL,
                profitability_rating REAL,
                valuation_rating REAL,
                sector TEXT,
                industry TEXT,
                adr_pct REAL,
                rs_3m REAL,
                short_float_pct REAL,
                next_earnings_date TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (date, symbol)
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
        await _ensure_columns(
            conn,
            "chartmill_universe",
            {
                "adr_pct": "REAL",
                "rs_3m": "REAL",
                "short_float_pct": "REAL",
            },
        )
        await _ensure_columns(
            conn,
            "signals",
            {
                "sim_outcome": "TEXT",
                "sim_exit_price": "REAL",
                "sim_pnl_r": "REAL",
                "sim_mfe_r": "REAL",
                "sim_mae_r": "REAL",
                "sim_evaluated_at": "TIMESTAMP",
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
        await conn.execute(
            """
            INSERT OR IGNORE INTO system_state (key, value)
            VALUES ('dead_mans_brake', 'OFF');
            """,
        )
        await conn.execute(
            """
            INSERT OR IGNORE INTO system_state (key, value)
            VALUES ('dead_mans_brake_reason', '');
            """,
        )

        # Financing & Paper-to-Live Qualification configuration (singleton row id = 1)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS financing_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                strategy_version TEXT NOT NULL DEFAULT 'orb_v1',
                lifecycle_stage TEXT NOT NULL DEFAULT 'PAPER_QUALIFICATION',
                risk_profile TEXT NOT NULL DEFAULT 'P2_BALANCED',
                seed_capital REAL NOT NULL DEFAULT 10000.0,
                target_capital_goal REAL NOT NULL DEFAULT 50000.0,
                reserve_pct REAL NOT NULL DEFAULT 20.0,
                max_live_accessible_capital REAL,
                pilot_risk_multiplier REAL NOT NULL DEFAULT 0.5,
                enforcement_enabled INTEGER NOT NULL DEFAULT 1,
                current_health_state TEXT NOT NULL DEFAULT 'HEALTHY',
                health_state_latched INTEGER NOT NULL DEFAULT 0,
                health_state_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                qual_min_trades INTEGER NOT NULL DEFAULT 30,
                qual_min_trading_days INTEGER NOT NULL DEFAULT 10,
                qual_min_expectancy_r REAL NOT NULL DEFAULT 0.15,
                qual_min_profit_factor REAL NOT NULL DEFAULT 1.30,
                qual_max_drawdown_pct REAL NOT NULL DEFAULT 10.0,
                qual_max_avg_slippage_r REAL NOT NULL DEFAULT 0.10,
                qual_max_ror_20pct REAL NOT NULL DEFAULT 10.0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await conn.execute(
            """
            INSERT OR IGNORE INTO financing_config (id) VALUES (1);
            """
        )

        # Capital Ledger: tracks deposits, withdrawals, vault sweeps & funding (separate from trading P&L)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capital_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_date TEXT NOT NULL,
                flow_type TEXT NOT NULL,
                amount REAL NOT NULL,
                direction TEXT NOT NULL,
                notes TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Manual / interactive live-readiness validation checks
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS validation_checks (
                check_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'NOT_RUN',
                notes TEXT DEFAULT '',
                verified_at TIMESTAMP
            );
            """
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


async def save_chartmill_universe(trade_date: str, candidates: List[Any]) -> int:
    """Persists per-symbol ChartMill technical & fundamental ratings for a trading day."""
    if not candidates:
        return 0
    async with get_connection() as conn:
        for c in candidates:
            await conn.execute(
                """
                INSERT INTO chartmill_universe (
                    date, symbol, company_name, price, atr_pct,
                    setup_rating, tech_rating, relative_strength, composite_score,
                    fundamental_rating, growth_rating, health_rating,
                    profitability_rating, valuation_rating, sector, industry,
                    adr_pct, rs_3m, short_float_pct, next_earnings_date, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(date, symbol) DO UPDATE SET
                    company_name = excluded.company_name,
                    price = excluded.price,
                    atr_pct = excluded.atr_pct,
                    setup_rating = excluded.setup_rating,
                    tech_rating = excluded.tech_rating,
                    relative_strength = excluded.relative_strength,
                    composite_score = excluded.composite_score,
                    fundamental_rating = excluded.fundamental_rating,
                    growth_rating = excluded.growth_rating,
                    health_rating = excluded.health_rating,
                    profitability_rating = excluded.profitability_rating,
                    valuation_rating = excluded.valuation_rating,
                    sector = excluded.sector,
                    industry = excluded.industry,
                    adr_pct = excluded.adr_pct,
                    rs_3m = excluded.rs_3m,
                    short_float_pct = excluded.short_float_pct,
                    next_earnings_date = excluded.next_earnings_date,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (
                    trade_date,
                    c.symbol,
                    c.company_name,
                    c.price,
                    c.atr_pct,
                    c.setup_rating,
                    c.tech_rating,
                    c.relative_strength,
                    c.composite_score,
                    c.fundamental_rating,
                    c.growth_rating,
                    c.health_rating,
                    c.profitability_rating,
                    c.valuation_rating,
                    c.sector,
                    c.industry,
                    getattr(c, "adr_pct", None),
                    getattr(c, "rs_3m", None),
                    getattr(c, "short_float_pct", None),
                    c.next_earnings_date,
                ),
            )
        await conn.commit()
    return len(candidates)


async def get_chartmill_profile(symbol: str, trade_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetches stored ChartMill ratings for a symbol (exact date first, then latest available)."""
    sym = symbol.upper().strip()
    async with get_connection() as conn:
        if trade_date:
            row = await (
                await conn.execute(
                    "SELECT * FROM chartmill_universe WHERE symbol = ? AND date = ? LIMIT 1;",
                    (sym, trade_date),
                )
            ).fetchone()
            if row:
                return dict(row)
        row = await (
            await conn.execute(
                "SELECT * FROM chartmill_universe WHERE symbol = ? ORDER BY date DESC LIMIT 1;",
                (sym,),
            )
        ).fetchone()
        return dict(row) if row else None


# Financing, Capital Ledger & Validation Check Helpers


FINANCING_CONFIG_COLUMNS = {
    "strategy_version",
    "lifecycle_stage",
    "risk_profile",
    "seed_capital",
    "target_capital_goal",
    "reserve_pct",
    "max_live_accessible_capital",
    "pilot_risk_multiplier",
    "enforcement_enabled",
    "current_health_state",
    "health_state_latched",
    "health_state_updated_at",
    "qual_min_trades",
    "qual_min_trading_days",
    "qual_min_expectancy_r",
    "qual_min_profit_factor",
    "qual_max_drawdown_pct",
    "qual_max_avg_slippage_r",
    "qual_max_ror_20pct",
}


async def get_financing_config() -> Dict[str, Any]:
    """Returns the singleton financing_config row, creating it if missing."""
    async with get_connection() as conn:
        await conn.execute("INSERT OR IGNORE INTO financing_config (id) VALUES (1);")
        await conn.commit()
        cursor = await conn.execute("SELECT * FROM financing_config WHERE id = 1;")
        row = await cursor.fetchone()
        return dict(row) if row else {}


async def update_financing_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Updates allowed columns on the singleton financing_config row."""
    clean = {k: v for k, v in updates.items() if k in FINANCING_CONFIG_COLUMNS}
    if not clean:
        return await get_financing_config()

    set_clauses = [f"{col} = ?" for col in clean]
    set_clauses.append("updated_at = CURRENT_TIMESTAMP")
    params = list(clean.values())

    async with get_connection() as conn:
        await conn.execute("INSERT OR IGNORE INTO financing_config (id) VALUES (1);")
        await conn.execute(
            f"UPDATE financing_config SET {', '.join(set_clauses)} WHERE id = 1;",
            params,
        )
        await conn.commit()
        cursor = await conn.execute("SELECT * FROM financing_config WHERE id = 1;")
        row = await cursor.fetchone()
        return dict(row) if row else {}


async def add_capital_ledger_entry(
    event_date: str,
    flow_type: str,
    amount: float,
    direction: str,
    notes: str = "",
) -> int:
    """Inserts a capital movement into capital_ledger and returns its row id."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO capital_ledger (event_date, flow_type, amount, direction, notes)
            VALUES (?, ?, ?, ?, ?);
            """,
            (event_date, flow_type, float(amount), direction, notes or ""),
        )
        await conn.commit()
        return int(cursor.lastrowid)


async def get_capital_ledger(limit: int = 200) -> List[Dict[str, Any]]:
    """Returns capital ledger entries ordered newest first."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM capital_ledger ORDER BY event_date DESC, id DESC LIMIT ?;",
            (limit,),
        )
        return [dict(r) for r in await cursor.fetchall()]


async def get_validation_checks() -> Dict[str, Dict[str, Any]]:
    """Returns stored manual validation checks keyed by check_id."""
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT * FROM validation_checks;")
        return {row["check_id"]: dict(row) for row in await cursor.fetchall()}


async def set_validation_check(check_id: str, status: str, notes: str = "") -> Dict[str, Any]:
    """Upserts a manual live-readiness validation check status."""
    verified_at = datetime.now(timezone.utc).isoformat() if status == "PASSED" else None
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO validation_checks (check_id, status, notes, verified_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(check_id) DO UPDATE SET
                status = excluded.status,
                notes = excluded.notes,
                verified_at = excluded.verified_at;
            """,
            (check_id, status, notes or "", verified_at),
        )
        await conn.commit()
        cursor = await conn.execute(
            "SELECT * FROM validation_checks WHERE check_id = ?;", (check_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else {"check_id": check_id, "status": status, "notes": notes}

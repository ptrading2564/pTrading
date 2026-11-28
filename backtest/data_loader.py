"""Rate-limited, SQLite-cached historical market data loader for Alpaca (1Day / 5Min / 1Min).

Supports both SIP (full consolidated historical bars, free on Alpaca Basic >15m old)
and IEX (matches live free real-time scanner feed). Uses split-adjusted bars so
historical stock splits do not distort ATR, SMA, or breakout levels.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time as time_mod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.config import settings
from backtest.calendar_data import generate_fallback_sessions
from backtest.config import get_backtest_cache_db_path, resolve_backtest_alpaca_credentials

logger = logging.getLogger(__name__)

ProgressCallback = Optional[Callable[[float, str], None]]


@dataclass(slots=True)
class Bar:
    """OHLCV bar duck-type compatible with MarketScanner indicator calculations."""

    timestamp: datetime  # timezone-aware UTC datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class TokenBucketRateLimiter:
    """Thread-safe token-bucket rate limiter keeping requests safely below Alpaca's 200 req/min cap."""

    def __init__(self, max_per_minute: int = 160) -> None:
        self.rate_per_sec = max_per_minute / 60.0
        self.capacity = float(max(10, max_per_minute // 4))
        self.tokens = self.capacity
        self.last_refill = time_mod.monotonic()

    def acquire(self) -> None:
        now = time_mod.monotonic()
        elapsed = now - self.last_refill
        self.last_refill = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
        if self.tokens < 1.0:
            sleep_for = (1.0 - self.tokens) / self.rate_per_sec
            time_mod.sleep(sleep_for)
            self.tokens = 0.0
            self.last_refill = time_mod.monotonic()
        else:
            self.tokens -= 1.0


class HistoricalDataStore:
    """Abstract historical data store interface (implemented by AlpacaCachedDataStore & InMemoryDataStore)."""

    async def get_trading_sessions(self, start_date: date, end_date: date) -> Dict[str, Dict[str, str]]:
        raise NotImplementedError

    async def get_bars(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start_dt: datetime,
        end_dt: datetime,
        feed: str = "sip",
        progress_cb: ProgressCallback = None,
    ) -> Dict[str, List[Bar]]:
        raise NotImplementedError


class InMemoryDataStore(HistoricalDataStore):
    """Deterministic in-memory bar store for unit tests and parity verification (zero network)."""

    def __init__(
        self,
        bars_by_tf: Optional[Dict[str, Dict[str, List[Bar]]]] = None,
        sessions: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> None:
        self.bars_by_tf: Dict[str, Dict[str, List[Bar]]] = bars_by_tf or {}
        self.sessions: Optional[Dict[str, Dict[str, str]]] = sessions

    async def get_trading_sessions(self, start_date: date, end_date: date) -> Dict[str, Dict[str, str]]:
        if self.sessions is not None:
            s_str = start_date.strftime("%Y-%m-%d")
            e_str = end_date.strftime("%Y-%m-%d")
            return {d: v for d, v in self.sessions.items() if s_str <= d <= e_str}
        return generate_fallback_sessions(start_date, end_date)

    async def get_bars(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start_dt: datetime,
        end_dt: datetime,
        feed: str = "sip",
        progress_cb: ProgressCallback = None,
    ) -> Dict[str, List[Bar]]:
        tf_map = self.bars_by_tf.get(timeframe, {})
        start_utc = start_dt.astimezone(timezone.utc)
        end_utc = end_dt.astimezone(timezone.utc)
        out: Dict[str, List[Bar]] = {}
        for sym in symbols:
            u = sym.upper()
            raw = tf_map.get(u, [])
            out[u] = [
                b for b in raw
                if start_utc <= b.timestamp.astimezone(timezone.utc) <= end_utc
            ]
        return out


class AlpacaCachedDataStore(HistoricalDataStore):
    """SQLite-cached, batched Alpaca Market Data loader for 1Day, 5Min, and 1Min US equity bars."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        cache_path: Optional[Path] = None,
        max_requests_per_min: int = 160,
    ) -> None:
        if api_key and secret_key:
            self.api_key = api_key
            self.secret_key = secret_key
            self.cred_source = "EXPLICIT"
        else:
            self.api_key, self.secret_key, self.cred_source = resolve_backtest_alpaca_credentials()

        self.cache_path = cache_path or get_backtest_cache_db_path()
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)
        self.limiter = TokenBucketRateLimiter(max_per_minute=max_requests_per_min)
        self._data_client = None
        self._trading_client = None
        self._init_cache_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.cache_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_cache_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    feed TEXT NOT NULL,
                    ts_utc INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    PRIMARY KEY (symbol, timeframe, feed, ts_utc)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coverage (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    feed TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    PRIMARY KEY (symbol, timeframe, feed)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calendar_sessions (
                    date TEXT PRIMARY KEY,
                    open_et TEXT NOT NULL,
                    close_et TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def _get_data_client(self):
        if self._data_client is None:
            if not self.api_key or not self.secret_key:
                raise RuntimeError(
                    "Alpaca API credentials missing. Set BACKTEST_ALPACA_API_KEY and "
                    "BACKTEST_ALPACA_SECRET_KEY (or ALPACA_API_KEY / ALPACA_SECRET_KEY) in .env."
                )
            from alpaca.data.historical import StockHistoricalDataClient

            self._data_client = StockHistoricalDataClient(
                api_key=self.api_key,
                secret_key=self.secret_key,
            )
        return self._data_client

    def _get_trading_client(self):
        if self._trading_client is None:
            if not self.api_key or not self.secret_key:
                return None
            from alpaca.trading.client import TradingClient

            self._trading_client = TradingClient(
                api_key=self.api_key,
                secret_key=self.secret_key,
                paper=True,
            )
        return self._trading_client

    def get_cache_stats(self) -> Dict[str, int]:
        """Returns cached bar counts by timeframe for the /backtest UI status banner."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT timeframe, COUNT(*) FROM bars GROUP BY timeframe;"
            ).fetchall()
            sym_count = conn.execute("SELECT COUNT(DISTINCT symbol) FROM bars;").fetchone()[0]
        counts = {str(r[0]): int(r[1]) for r in rows}
        counts["distinct_symbols"] = int(sym_count or 0)
        return counts

    async def get_trading_sessions(self, start_date: date, end_date: date) -> Dict[str, Dict[str, str]]:
        """Fetches official NYSE trading sessions (including early closes) from cache or Alpaca."""
        return await asyncio.to_thread(self._get_trading_sessions_sync, start_date, end_date)

    def _get_trading_sessions_sync(self, start_date: date, end_date: date) -> Dict[str, Dict[str, str]]:
        s_str = start_date.strftime("%Y-%m-%d")
        e_str = end_date.strftime("%Y-%m-%d")

        # Check cache first
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date, open_et, close_et FROM calendar_sessions WHERE date >= ? AND date <= ? ORDER BY date ASC;",
                (s_str, e_str),
            ).fetchall()

        # Heuristic: if we already have sessions spanning near both ends, reuse cache
        expected_weekdays = sum(
            1
            for i in range((end_date - start_date).days + 1)
            if (start_date + timedelta(days=i)).weekday() < 5
        )
        if rows and len(rows) >= max(1, int(expected_weekdays * 0.85)):
            return {r[0]: {"date": r[0], "open": r[1], "close": r[2]} for r in rows}

        client = self._get_trading_client()
        if client is None:
            return generate_fallback_sessions(start_date, end_date)

        try:
            from alpaca.trading.requests import GetCalendarRequest

            self.limiter.acquire()
            cal = client.get_calendar(filters=GetCalendarRequest(start=start_date, end=end_date))
            sessions: Dict[str, Dict[str, str]] = {}
            to_store: List[Tuple[str, str, str]] = []
            for d_info in cal or []:
                d_s = d_info.date.strftime("%Y-%m-%d") if hasattr(d_info.date, "strftime") else str(d_info.date)[:10]
                o_s = d_info.open.strftime("%H:%M") if hasattr(d_info.open, "strftime") else str(d_info.open)[:5]
                c_s = d_info.close.strftime("%H:%M") if hasattr(d_info.close, "strftime") else str(d_info.close)[:5]
                sessions[d_s] = {"date": d_s, "open": o_s, "close": c_s}
                to_store.append((d_s, o_s, c_s))

            if to_store:
                with self._connect() as conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO calendar_sessions (date, open_et, close_et) VALUES (?, ?, ?);",
                        to_store,
                    )
                    conn.commit()
            return sessions
        except Exception as e:
            logger.warning("Alpaca calendar fetch failed (%s); falling back to offline NYSE calendar.", e)
            return generate_fallback_sessions(start_date, end_date)

    async def get_bars(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start_dt: datetime,
        end_dt: datetime,
        feed: str = "sip",
        progress_cb: ProgressCallback = None,
    ) -> Dict[str, List[Bar]]:
        """Loads requested bars from SQLite cache, fetching only missing symbol/date ranges from Alpaca."""
        return await asyncio.to_thread(
            self._get_bars_sync,
            list(dict.fromkeys(s.strip().upper() for s in symbols if s and s.strip())),
            timeframe,
            start_dt,
            end_dt,
            feed.lower(),
            progress_cb,
        )

    @staticmethod
    def _alpaca_timeframe(tf_str: str):
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        if tf_str == "1Day":
            return TimeFrame(1, TimeFrameUnit.Day)
        if tf_str == "5Min":
            return TimeFrame(5, TimeFrameUnit.Minute)
        if tf_str == "1Min":
            return TimeFrame(1, TimeFrameUnit.Minute)
        raise ValueError(f"Unsupported timeframe: {tf_str}")

    def _get_bars_sync(
        self,
        symbols: List[str],
        timeframe: str,
        start_dt: datetime,
        end_dt: datetime,
        feed: str,
        progress_cb: ProgressCallback,
    ) -> Dict[str, List[Bar]]:
        if not symbols:
            return {}

        start_utc = start_dt.astimezone(timezone.utc)
        end_utc = end_dt.astimezone(timezone.utc)

        # Alpaca Basic free plan blocks SIP queries within the last 15 minutes
        if feed == "sip":
            sip_safe_end = datetime.now(timezone.utc) - timedelta(minutes=16)
            if end_utc > sip_safe_end:
                end_utc = sip_safe_end

        if end_utc <= start_utc:
            return {s: [] for s in symbols}

        req_start_d = start_utc.astimezone(self.tz).strftime("%Y-%m-%d")
        req_end_d = end_utc.astimezone(self.tz).strftime("%Y-%m-%d")

        # Determine which symbols need fetching (missing or partial date coverage)
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in symbols)
            cov_rows = conn.execute(
                f"SELECT symbol, start_date, end_date FROM coverage WHERE timeframe = ? AND feed = ? AND symbol IN ({placeholders});",
                [timeframe, feed, *symbols],
            ).fetchall()
        cov_map: Dict[str, Tuple[str, str]] = {r[0]: (r[1], r[2]) for r in cov_rows}

        symbols_to_fetch: List[str] = []
        for sym in symbols:
            cov = cov_map.get(sym)
            if cov is None or req_start_d < cov[0] or req_end_d > cov[1]:
                symbols_to_fetch.append(sym)

        if symbols_to_fetch:
            self._fetch_and_cache_batches(
                symbols=symbols_to_fetch,
                timeframe=timeframe,
                start_utc=start_utc,
                end_utc=end_utc,
                req_start_d=req_start_d,
                req_end_d=req_end_d,
                cov_map=cov_map,
                feed=feed,
                progress_cb=progress_cb,
            )

        # Read requested range cleanly from SQLite cache
        start_ts = int(start_utc.timestamp())
        end_ts = int(end_utc.timestamp())
        result: Dict[str, List[Bar]] = {s: [] for s in symbols}

        with self._connect() as conn:
            chunk_size = 400
            for i in range(0, len(symbols), chunk_size):
                chunk = symbols[i : i + chunk_size]
                ph = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT symbol, ts_utc, open, high, low, close, volume
                    FROM bars
                    WHERE timeframe = ? AND feed = ? AND symbol IN ({ph})
                      AND ts_utc >= ? AND ts_utc <= ?
                    ORDER BY symbol ASC, ts_utc ASC;
                    """,
                    [timeframe, feed, *chunk, start_ts, end_ts],
                ).fetchall()
                for sym, ts_u, o, h, l, c, v in rows:
                    result[sym].append(
                        Bar(
                            timestamp=datetime.fromtimestamp(int(ts_u), tz=timezone.utc),
                            open=float(o),
                            high=float(h),
                            low=float(l),
                            close=float(c),
                            volume=float(v),
                        )
                    )
        return result

    def _fetch_and_cache_batches(
        self,
        symbols: List[str],
        timeframe: str,
        start_utc: datetime,
        end_utc: datetime,
        req_start_d: str,
        req_end_d: str,
        cov_map: Dict[str, Tuple[str, str]],
        feed: str,
        progress_cb: ProgressCallback,
    ) -> None:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest

        client = self._get_data_client()
        alpaca_tf = self._alpaca_timeframe(timeframe)
        data_feed = DataFeed.IEX if feed == "iex" else DataFeed.SIP

        # Choose safe batch sizes to avoid huge pagination chains per call
        if timeframe == "1Day":
            batch_size = 50
            window_days = 400
        elif timeframe == "5Min":
            batch_size = 20
            window_days = 45
        else:  # 1Min
            batch_size = 10
            window_days = 15

        # Split time window into manageable chunks for progress reporting and resilience
        windows: List[Tuple[datetime, datetime]] = []
        cur_s = start_utc
        while cur_s < end_utc:
            cur_e = min(end_utc, cur_s + timedelta(days=window_days))
            windows.append((cur_s, cur_e))
            cur_s = cur_e

        batches = [symbols[i : i + batch_size] for i in range(0, len(symbols), batch_size)]
        total_steps = max(1, len(batches) * len(windows))
        step_idx = 0

        for b_idx, batch in enumerate(batches):
            rows_to_insert: List[Tuple[str, str, str, int, float, float, float, float, float]] = []
            for w_start, w_end in windows:
                step_idx += 1
                if progress_cb:
                    pct = (step_idx / total_steps) * 100.0
                    progress_cb(
                        pct,
                        f"Fetching {timeframe} ({feed.upper()}) batch {b_idx + 1}/{len(batches)} "
                        f"[{w_start.strftime('%Y-%m-%d')}..{w_end.strftime('%Y-%m-%d')}]",
                    )

                req = StockBarsRequest(
                    symbol_or_symbols=batch,
                    timeframe=alpaca_tf,
                    start=w_start,
                    end=w_end,
                    feed=data_feed,
                    adjustment=Adjustment.SPLIT,
                )

                barset = self._request_with_retry(client, req)
                if barset is None:
                    continue

                data_dict = getattr(barset, "data", {}) or {}
                for sym, sym_bars in data_dict.items():
                    sym_u = str(sym).upper()
                    for b in sym_bars:
                        ts = b.timestamp
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        rows_to_insert.append(
                            (
                                sym_u,
                                timeframe,
                                feed,
                                int(ts.timestamp()),
                                float(b.open),
                                float(b.high),
                                float(b.low),
                                float(b.close),
                                float(b.volume or 0.0),
                            )
                        )

            # Persist batch bars + updated coverage atomically
            cov_updates: List[Tuple[str, str, str, str, str]] = []
            for sym in batch:
                old = cov_map.get(sym)
                new_s = min(req_start_d, old[0]) if old else req_start_d
                new_e = max(req_end_d, old[1]) if old else req_end_d
                cov_updates.append((sym, timeframe, feed, new_s, new_e))

            with self._connect() as conn:
                if rows_to_insert:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO bars
                        (symbol, timeframe, feed, ts_utc, open, high, low, close, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        rows_to_insert,
                    )
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO coverage (symbol, timeframe, feed, start_date, end_date)
                    VALUES (?, ?, ?, ?, ?);
                    """,
                    cov_updates,
                )
                conn.commit()

    def _request_with_retry(self, client, req, max_retries: int = 4):
        backoff = 2.0
        for attempt in range(max_retries):
            self.limiter.acquire()
            try:
                return client.get_stock_bars(req)
            except Exception as e:
                msg = str(e).lower()
                is_rate_limit = "429" in msg or "too many requests" in msg or "rate" in msg
                is_transient = is_rate_limit or "timeout" in msg or "502" in msg or "503" in msg or "connection" in msg
                if attempt < max_retries - 1 and is_transient:
                    wait = backoff * (2 ** attempt)
                    logger.warning("Alpaca data fetch transient error (%s). Retrying in %.1fs...", e, wait)
                    time_mod.sleep(wait)
                    continue
                logger.error("Alpaca data fetch failed permanently: %s", e)
                raise
        return None

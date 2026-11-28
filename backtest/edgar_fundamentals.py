"""Optional free point-in-time SEC EDGAR XBRL Fundamental Rating proxy (0-10).

Strictly gated by SEC `filed` date (< target_date) so historical fundamental
scores have zero look-ahead bias. Results are cached permanently in SQLite
(`data/backtest_cache.db`) to respect SEC EDGAR's 10 req/sec fair-access policy.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time as time_mod
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import httpx

from backtest.config import get_backtest_cache_db_path

logger = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_USER_AGENT = "pTradingBacktester/1.0 (contact: p.trading2564@gmail.com)"


class EdgarFundamentalProvider:
    """Fetches and caches point-in-time fundamental snapshots from free SEC EDGAR XBRL API."""

    def __init__(self, cache_path: Optional[Path] = None) -> None:
        self.cache_path = cache_path or get_backtest_cache_db_path()
        self._cik_map: Optional[Dict[str, str]] = None
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.cache_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS edgar_cik_map (
                    symbol TEXT PRIMARY KEY,
                    cik TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS edgar_fundamental_points (
                    symbol TEXT NOT NULL,
                    filed_date TEXT NOT NULL,
                    fundamental_rating REAL NOT NULL,
                    growth_score REAL,
                    profitability_score REAL,
                    health_score REAL,
                    PRIMARY KEY (symbol, filed_date)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS edgar_fetched_symbols (
                    symbol TEXT PRIMARY KEY,
                    fetched_at INTEGER NOT NULL
                );
                """
            )
            conn.commit()

    def _ensure_cik_map(self) -> Dict[str, str]:
        if self._cik_map is not None:
            return self._cik_map

        with self._connect() as conn:
            rows = conn.execute("SELECT symbol, cik FROM edgar_cik_map;").fetchall()
        if rows:
            self._cik_map = {str(r[0]).upper(): str(r[1]) for r in rows}
            return self._cik_map

        try:
            res = httpx.get(
                SEC_TICKERS_URL,
                headers={"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"},
                timeout=15.0,
            )
            res.raise_for_status()
            raw = res.json()
            mapping: Dict[str, str] = {}
            for entry in raw.values():
                ticker = str(entry.get("ticker", "")).strip().upper()
                cik_num = entry.get("cik_str")
                if ticker and cik_num is not None:
                    mapping[ticker] = str(int(cik_num)).zfill(10)
            with self._connect() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO edgar_cik_map (symbol, cik) VALUES (?, ?);",
                    list(mapping.items()),
                )
                conn.commit()
            self._cik_map = mapping
            return mapping
        except Exception as e:
            logger.warning("Could not fetch SEC CIK map (%s); fundamental proxy disabled.", e)
            self._cik_map = {}
            return self._cik_map

    @staticmethod
    def _extract_series(us_gaap: dict, tag_candidates: List[str], unit: str = "USD") -> List[Tuple[str, str, float]]:
        """Returns list of (filed_date, end_date, value) sorted by filed_date."""
        for tag in tag_candidates:
            tag_obj = us_gaap.get(tag)
            if not tag_obj:
                continue
            units = tag_obj.get("units", {})
            items = units.get(unit)
            if not items:
                continue
            pts: List[Tuple[str, str, float]] = []
            for it in items:
                filed = str(it.get("filed") or "")[:10]
                end = str(it.get("end") or "")[:10]
                val = it.get("val")
                form = str(it.get("form") or "")
                if filed and end and val is not None and form in ("10-K", "10-Q"):
                    try:
                        pts.append((filed, end, float(val)))
                    except (TypeError, ValueError):
                        pass
            if pts:
                pts.sort(key=lambda x: (x[0], x[1]))
                return pts
        return []

    def _fetch_symbol_facts(self, symbol: str) -> None:
        sym_u = symbol.upper()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM edgar_fetched_symbols WHERE symbol = ?;", (sym_u,)
            ).fetchone()
            if row:
                return

        cik_map = self._ensure_cik_map()
        cik = cik_map.get(sym_u)
        if not cik:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO edgar_fetched_symbols (symbol, fetched_at) VALUES (?, ?);",
                    (sym_u, int(time_mod.time())),
                )
                conn.commit()
            return

        # Respect SEC EDGAR fair access (<10 req/sec)
        time_mod.sleep(0.15)
        try:
            res = httpx.get(
                SEC_FACTS_URL.format(cik=cik),
                headers={"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"},
                timeout=15.0,
            )
            if res.status_code != 200:
                raise RuntimeError(f"HTTP {res.status_code}")
            facts = res.json().get("facts", {}).get("us-gaap", {})
        except Exception as e:
            logger.debug("SEC EDGAR facts fetch failed for %s: %s", sym_u, e)
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO edgar_fetched_symbols (symbol, fetched_at) VALUES (?, ?);",
                    (sym_u, int(time_mod.time())),
                )
                conn.commit()
            return

        rev = self._extract_series(
            facts,
            [
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
            ],
        )
        op_inc = self._extract_series(facts, ["OperatingIncomeLoss"])
        net_inc = self._extract_series(facts, ["NetIncomeLoss"])
        cur_assets = self._extract_series(facts, ["AssetsCurrent"])
        cur_liab = self._extract_series(facts, ["LiabilitiesCurrent"])
        equity = self._extract_series(facts, ["StockholdersEquity"])
        debt = self._extract_series(facts, ["LongTermDebt", "LongTermDebtNoncurrent", "DebtCurrent"])

        filed_dates = sorted({p[0] for p in (rev + net_inc + cur_assets + equity) if p[0] >= "2019-01-01"})

        def latest_as_of(series: List[Tuple[str, str, float]], as_of_filed: str) -> Optional[Tuple[str, str, float]]:
            valid = [p for p in series if p[0] <= as_of_filed]
            return valid[-1] if valid else None

        def prior_year_val(series: List[Tuple[str, str, float]], as_of_filed: str, cur_end: str) -> Optional[float]:
            valid = [p for p in series if p[0] <= as_of_filed and p[1] < cur_end]
            # Find period roughly ~1 year before cur_end
            for p in reversed(valid):
                try:
                    d_cur = date.fromisoformat(cur_end)
                    d_old = date.fromisoformat(p[1])
                    delta = (d_cur - d_old).days
                    if 320 <= delta <= 410:
                        return p[2]
                except ValueError:
                    continue
            return None

        rows_to_store: List[Tuple[str, str, float, float, float, float]] = []
        for fd in filed_dates:
            # 1. Growth score (0-10)
            growth_score = 5.0
            r_cur = latest_as_of(rev, fd)
            if r_cur and r_cur[2] > 0:
                r_prev = prior_year_val(rev, fd, r_cur[1])
                if r_prev and r_prev > 0:
                    yoy = (r_cur[2] - r_prev) / r_prev
                    growth_score = max(0.0, min(10.0, 5.0 + yoy * 15.0))

            # 2. Profitability score (0-10)
            prof_score = 5.0
            ni_cur = latest_as_of(net_inc, fd)
            oi_cur = latest_as_of(op_inc, fd)
            if r_cur and r_cur[2] > 0 and oi_cur:
                margin = oi_cur[2] / r_cur[2]
                prof_score = max(0.0, min(10.0, 4.0 + margin * 20.0))
            elif ni_cur:
                prof_score = 7.0 if ni_cur[2] > 0 else 2.5

            # 3. Financial health score (0-10)
            health_score = 5.0
            ca = latest_as_of(cur_assets, fd)
            cl = latest_as_of(cur_liab, fd)
            eq = latest_as_of(equity, fd)
            dt = latest_as_of(debt, fd)
            sub_scores: List[float] = []
            if ca and cl and cl[2] > 0:
                cr = ca[2] / cl[2]
                sub_scores.append(max(0.0, min(10.0, cr * 4.0)))
            if eq and eq[2] > 0 and dt:
                dte = max(0.0, dt[2] / eq[2])
                sub_scores.append(max(0.0, min(10.0, 9.0 - dte * 3.0)))
            if sub_scores:
                health_score = sum(sub_scores) / len(sub_scores)

            overall = round((growth_score * 0.40) + (prof_score * 0.35) + (health_score * 0.25), 1)
            rows_to_store.append((sym_u, fd, overall, round(growth_score, 1), round(prof_score, 1), round(health_score, 1)))

        with self._connect() as conn:
            if rows_to_store:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO edgar_fundamental_points
                    (symbol, filed_date, fundamental_rating, growth_score, profitability_score, health_score)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    rows_to_store,
                )
            conn.execute(
                "INSERT OR REPLACE INTO edgar_fetched_symbols (symbol, fetched_at) VALUES (?, ?);",
                (sym_u, int(time_mod.time())),
            )
            conn.commit()

    def get_point_in_time_rating(self, symbol: str, as_of_date: date) -> Optional[float]:
        """Returns latest SEC EDGAR Fundamental Rating (0-10) strictly filed BEFORE as_of_date."""
        sym_u = symbol.upper()
        self._fetch_symbol_facts(sym_u)
        cutoff = as_of_date.strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT fundamental_rating FROM edgar_fundamental_points
                WHERE symbol = ? AND filed_date < ?
                ORDER BY filed_date DESC
                LIMIT 1;
                """,
                (sym_u, cutoff),
            ).fetchone()
        return float(row[0]) if row else None

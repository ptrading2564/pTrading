"""Point-in-time historical screener simulator supporting Mode A, Mode B, and Mode C.

Mode A ('screener'):
  Reconstructs each trading morning's momentum breakout basket strictly from
  daily bars through day T-1 close (zero look-ahead bias).
  Exact rules: Price >= $10, 50d Avg Vol >= 1M, 14d ATR% >= 2.5%, Price > SMA20 > SMA50,
  cross-sectional RS >= 75.
  Quantitative proxies: ChartMill Technical Rating (0-10), Setup Rating (0-10),
  and optional point-in-time SEC EDGAR Fundamental Rating (0-10).
  Ranking reuses live ChartMillImporter.calculate_composite_score() read-only.

Mode B ('fixed'):
  Replays settings.DEFAULT_BASKET (or custom symbols) with point-in-time daily metrics.

Mode C ('chartmill_history'):
  Read-only replay of real uploaded ChartMill CSV snapshots saved in live
  data/ptrading.db (`chartmill_universe`).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.chartmill_importer import chartmill_importer
from backtest.config import BacktestConfig
from backtest.data_loader import Bar

logger = logging.getLogger(__name__)


@dataclass
class ScreenedCandidate:
    symbol: str
    price: float
    avg_vol_50: float
    atr_pct: float
    relative_strength: float
    tech_rating: float
    setup_rating: float
    composite_score: float
    fundamental_rating: Optional[float] = None
    sector: Optional[str] = None
    source: str = "mode_a_proxy"


def _sma(vals: Sequence[float], period: int) -> Optional[float]:
    if len(vals) < period or period <= 0:
        return None
    return sum(vals[-period:]) / period


def _atr_daily(bars: Sequence[Bar], period: int = 14) -> Optional[float]:
    if len(bars) < 2:
        return None
    trs: List[float] = []
    start_idx = max(1, len(bars) - period)
    for i in range(start_idx, len(bars)):
        h = bars[i].high
        l = bars[i].low
        pc = bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return (sum(trs) / len(trs)) if trs else None


def _compute_weighted_momentum_raw(closes: Sequence[float]) -> Optional[float]:
    """IBD/ChartMill weighted multi-horizon return score (3m=40%, 6m=20%, 9m=20%, 12m=20%)."""
    n = len(closes)
    if n < 22 or closes[-1] <= 0:
        return None
    c_now = closes[-1]

    components: List[Tuple[float, float]] = []
    for lookback, weight in ((63, 0.40), (126, 0.20), (189, 0.20), (252, 0.20)):
        if n > lookback and closes[-1 - lookback] > 0:
            ret = (c_now / closes[-1 - lookback]) - 1.0
            components.append((ret, weight))

    # Graceful fallback for recent IPOs with 22-62 daily bars
    if not components and n >= 22 and closes[0] > 0:
        return (c_now / closes[0]) - 1.0

    total_w = sum(w for _, w in components)
    if total_w <= 0:
        return None
    return sum(r * w for r, w in components) / total_w


def compute_tech_rating_proxy(bars: Sequence[Bar], rs_percentile: float) -> float:
    """Quantitative 0-10 proxy for ChartMill Technical Rating based on disclosed trend/RS inputs."""
    closes = [b.close for b in bars]
    c = closes[-1]
    sma10 = _sma(closes, 10)
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)

    score = 0.0

    # 1. Moving average stack alignment (0 - 3.0 pts)
    if sma10 and c > sma10:
        score += 0.6
    if sma20 and c > sma20:
        score += 0.8
    if sma20 and sma50 and sma20 > sma50:
        score += 0.8
    if sma200:
        if c > sma200:
            score += 0.4
        if sma50 and sma50 > sma200:
            score += 0.4
    elif sma50 and c > sma50:
        score += 0.6

    # 2. Moving average slopes (0 - 2.0 pts)
    if len(closes) >= 25:
        sma20_prev = _sma(closes[:-5], 20)
        if sma20 and sma20_prev and sma20 > sma20_prev:
            score += 1.0
    if len(closes) >= 60:
        sma50_prev = _sma(closes[:-10], 50)
        if sma50 and sma50_prev and sma50 > sma50_prev:
            score += 1.0
    elif len(closes) >= 25:
        score += 0.5

    # 3. Proximity to 52-week high (0 - 2.0 pts)
    high_window = bars[-252:] if len(bars) >= 252 else bars
    hi_52w = max(b.high for b in high_window)
    if hi_52w > 0:
        ratio = c / hi_52w
        # >= 0.95 -> 2.0 pts, 0.70 -> 0.0 pts
        prox_pts = max(0.0, min(2.0, ((ratio - 0.70) / 0.25) * 2.0))
        score += prox_pts

    # 4. Relative Strength percentile (0 - 3.0 pts)
    score += max(0.0, min(3.0, (rs_percentile / 100.0) * 3.0))

    return round(max(0.0, min(10.0, score)), 1)


def compute_setup_rating_proxy(bars: Sequence[Bar]) -> float:
    """Quantitative 0-10 proxy for ChartMill Setup Rating (base tightness, VCP contraction, proximity to MA)."""
    if len(bars) < 20:
        return 5.0

    closes = [b.close for b in bars]
    c = closes[-1]
    sma20 = _sma(closes, 20) or c
    atr5 = _atr_daily(bars, 5) or 0.0
    atr20 = _atr_daily(bars, 20) or 0.0

    score = 0.0

    # 1. Volatility Contraction (ATR5 / ATR20): tighter recent bars score higher (0 - 3.0 pts)
    if atr20 > 0:
        vcp_ratio = atr5 / atr20
        # 0.60 or lower -> 3.0 pts; 1.35 or higher -> 0.0 pts
        vcp_pts = max(0.0, min(3.0, ((1.35 - vcp_ratio) / 0.75) * 3.0))
        score += vcp_pts
    else:
        score += 1.5

    # 2. 10-day consolidation tightness near recent high (0 - 2.5 pts)
    last10 = bars[-10:]
    hi10 = max(b.high for b in last10)
    lo10 = min(b.low for b in last10)
    range10_pct = ((hi10 - lo10) / c) * 100.0 if c > 0 else 20.0
    # Tight 4%-14% 10-day base scores high; sloppy >22% scores low
    tight_pts = max(0.0, min(2.5, ((22.0 - range10_pct) / 14.0) * 2.5))
    score += tight_pts

    # 3. Not overextended above SMA20 (sweet spot: 0 to +2.0 ATR20 above SMA20) (0 - 2.5 pts)
    if atr20 > 0:
        ext_atr = (c - sma20) / atr20
        if -0.5 <= ext_atr <= 2.0:
            score += 2.5
        elif 2.0 < ext_atr <= 4.0:
            score += max(0.0, 2.5 * (1.0 - (ext_atr - 2.0) / 2.0))
        elif -1.5 <= ext_atr < -0.5:
            score += 1.0
    else:
        score += 1.5

    # 4. Volume dry-up / controlled supply in last 5 days vs 20-day average (0 - 2.0 pts)
    vol5 = sum(b.volume for b in bars[-5:]) / 5.0
    vol20 = sum(b.volume for b in bars[-20:]) / 20.0
    if vol20 > 0:
        vol_ratio = vol5 / vol20
        # Volume <= 0.85x avg -> 2.0 pts; >= 1.5x avg -> 0.2 pts
        vol_pts = max(0.2, min(2.0, ((1.50 - vol_ratio) / 0.65) * 2.0))
        score += vol_pts
    else:
        score += 1.0

    return round(max(0.0, min(10.0, score)), 1)


class ScreenerSimulator:
    """Produces point-in-time daily trading baskets for Mode A, Mode B, and Mode C."""

    def __init__(
        self,
        cfg: BacktestConfig,
        daily_bars_by_symbol: Dict[str, List[Bar]],
        edgar_provider=None,
    ) -> None:
        self.cfg = cfg
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)
        self.edgar_provider = edgar_provider

        # Pre-index daily bars by trading date string per symbol for O(1) prefix lookups
        self._bars_by_sym: Dict[str, List[Bar]] = {}
        self._dates_by_sym: Dict[str, List[str]] = {}
        for sym, bars in daily_bars_by_symbol.items():
            sym_u = sym.upper()
            sorted_bars = sorted(bars, key=lambda b: b.timestamp)
            self._bars_by_sym[sym_u] = sorted_bars
            self._dates_by_sym[sym_u] = [
                b.timestamp.astimezone(self.tz).strftime("%Y-%m-%d") for b in sorted_bars
            ]

    def _bars_strictly_before(self, symbol: str, target_date_str: str) -> List[Bar]:
        """Returns daily bars strictly before target_date_str (zero look-ahead)."""
        dates = self._dates_by_sym.get(symbol)
        if not dates:
            return []
        # Binary search for first index >= target_date_str
        lo, hi = 0, len(dates)
        while lo < hi:
            mid = (lo + hi) // 2
            if dates[mid] < target_date_str:
                lo = mid + 1
            else:
                hi = mid
        return self._bars_by_sym[symbol][:lo]

    def screen_for_date(self, target_date: date) -> List[ScreenedCandidate]:
        """Generates the ranked trading basket strictly before market open on target_date."""
        mode = self.cfg.mode.lower()
        if mode == "fixed":
            return self._screen_mode_b_fixed(target_date)
        if mode == "chartmill_history":
            return self._screen_mode_c_chartmill_history(target_date)
        return self._screen_mode_a_dynamic(target_date)

    def _screen_mode_a_dynamic(self, target_date: date) -> List[ScreenedCandidate]:
        d_str = target_date.strftime("%Y-%m-%d")

        # Pass 1: compute raw weighted momentum across universe for true cross-sectional RS percentiles
        raw_rs: Dict[str, float] = {}
        prefixes: Dict[str, List[Bar]] = {}
        for sym in self._bars_by_sym:
            if sym in ("SPY", "QQQ"):
                continue
            hist = self._bars_strictly_before(sym, d_str)
            if len(hist) < 50:
                continue
            prefixes[sym] = hist
            mom = _compute_weighted_momentum_raw([b.close for b in hist])
            if mom is not None:
                raw_rs[sym] = mom

        if not raw_rs:
            return []

        # Convert raw momentum scores into 0-100 cross-sectional percentile ranks
        sorted_syms = sorted(raw_rs.keys(), key=lambda s: raw_rs[s])
        n_rs = len(sorted_syms)
        rs_pct_map: Dict[str, float] = {}
        for idx, sym in enumerate(sorted_syms):
            pct = (idx / max(1, n_rs - 1)) * 100.0 if n_rs > 1 else 80.0
            rs_pct_map[sym] = round(pct, 1)

        # Pass 2: apply exact liquidity/volatility/trend filters + Technical/Setup proxies
        candidates: List[ScreenedCandidate] = []
        for sym, hist in prefixes.items():
            rs = rs_pct_map.get(sym, 0.0)
            if rs < self.cfg.screener_min_rs:
                continue

            closes = [b.close for b in hist]
            price = closes[-1]
            if price < self.cfg.screener_min_price:
                continue

            avg_vol_50 = sum(b.volume for b in hist[-50:]) / 50.0
            if avg_vol_50 < self.cfg.screener_min_avg_vol:
                continue

            sma20 = _sma(closes, 20)
            sma50 = _sma(closes, 50)
            if not (sma20 and sma50 and price > sma20 and sma20 > sma50):
                continue

            atr14 = _atr_daily(hist, 14)
            if not atr14 or price <= 0:
                continue
            atr_pct = round((atr14 / price) * 100.0, 2)
            if atr_pct < self.cfg.screener_min_atr_pct:
                continue

            tech_rating = compute_tech_rating_proxy(hist, rs)
            if tech_rating < self.cfg.screener_min_tech_rating:
                continue

            setup_rating = compute_setup_rating_proxy(hist)
            if setup_rating < self.cfg.screener_min_setup_rating:
                continue

            fund_rating: Optional[float] = None
            if self.cfg.enable_edgar_fundamentals and self.edgar_provider is not None:
                fund_rating = self.edgar_provider.get_point_in_time_rating(sym, target_date)
                if fund_rating is not None and fund_rating < self.cfg.screener_min_fundamental_rating:
                    continue

            # Reuse live ChartMillImporter composite ranking formula directly (read-only)
            composite = chartmill_importer.calculate_composite_score(
                setup_rating=setup_rating,
                tech_rating=tech_rating,
                relative_strength=rs,
                atr_pct=atr_pct,
            )

            candidates.append(
                ScreenedCandidate(
                    symbol=sym,
                    price=round(price, 2),
                    avg_vol_50=round(avg_vol_50, 0),
                    atr_pct=atr_pct,
                    relative_strength=rs,
                    tech_rating=tech_rating,
                    setup_rating=setup_rating,
                    composite_score=composite,
                    fundamental_rating=fund_rating,
                    source="mode_a_proxy",
                )
            )

        candidates.sort(key=lambda c: c.composite_score, reverse=True)
        if self.cfg.max_basket_size is not None and self.cfg.max_basket_size > 0:
            return candidates[: self.cfg.max_basket_size]
        return candidates

    def _screen_mode_b_fixed(self, target_date: date) -> List[ScreenedCandidate]:
        d_str = target_date.strftime("%Y-%m-%d")
        fixed_syms = self.cfg.resolved_fixed_symbols()
        if self.cfg.max_basket_size is not None and self.cfg.max_basket_size > 0:
            fixed_syms = fixed_syms[: self.cfg.max_basket_size]

        out: List[ScreenedCandidate] = []
        total = max(1, len(fixed_syms))
        for idx, sym in enumerate(fixed_syms):
            hist = self._bars_strictly_before(sym, d_str)
            price = hist[-1].close if hist else 0.0
            atr14 = _atr_daily(hist, 14) if len(hist) >= 2 else None
            atr_pct = round((atr14 / price) * 100.0, 2) if (atr14 and price > 0) else 3.0
            avg_vol = (sum(b.volume for b in hist[-50:]) / min(len(hist), 50)) if hist else 0.0
            tech = compute_tech_rating_proxy(hist, 80.0) if len(hist) >= 20 else 8.0
            setup = compute_setup_rating_proxy(hist) if len(hist) >= 20 else 7.5
            # Preserve fixed basket priority order via descending composite_score
            order_score = round(10.0 - (idx / total), 3)
            out.append(
                ScreenedCandidate(
                    symbol=sym,
                    price=round(price, 2),
                    avg_vol_50=round(avg_vol, 0),
                    atr_pct=atr_pct,
                    relative_strength=80.0,
                    tech_rating=tech,
                    setup_rating=setup,
                    composite_score=order_score,
                    source="mode_b_fixed",
                )
            )
        return out

    def _screen_mode_c_chartmill_history(self, target_date: date) -> List[ScreenedCandidate]:
        """Read-only lookup of real ChartMill CSV uploads saved in live data/ptrading.db."""
        d_str = target_date.strftime("%Y-%m-%d")
        db_path = settings.DATABASE_PATH
        if not db_path.exists():
            return self._screen_mode_b_fixed(target_date)

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                # Find the latest uploaded ChartMill date <= target_date
                row = conn.execute(
                    "SELECT MAX(date) as d FROM chartmill_universe WHERE date <= ?;",
                    (d_str,),
                ).fetchone()
                cm_date = row["d"] if row and row["d"] else None
                if not cm_date:
                    return []

                rows = conn.execute(
                    """
                    SELECT symbol, price, atr_pct, setup_rating, tech_rating, relative_strength,
                           composite_score, fundamental_rating, sector, next_earnings_date
                    FROM chartmill_universe
                    WHERE date = ?
                    ORDER BY composite_score DESC;
                    """,
                    (cm_date,),
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Read-only chartmill_universe query failed (%s).", e)
            return []

        out: List[ScreenedCandidate] = []
        for r in rows:
            is_risk, _ = chartmill_importer.is_earnings_risk(r["next_earnings_date"], reference_date=target_date)
            if is_risk:
                continue
            out.append(
                ScreenedCandidate(
                    symbol=str(r["symbol"]).upper(),
                    price=float(r["price"] or 0.0),
                    avg_vol_50=0.0,
                    atr_pct=float(r["atr_pct"] or 0.0),
                    relative_strength=float(r["relative_strength"] or 0.0),
                    tech_rating=float(r["tech_rating"] or 0.0),
                    setup_rating=float(r["setup_rating"] or 0.0),
                    composite_score=float(r["composite_score"] or 0.0),
                    fundamental_rating=float(r["fundamental_rating"]) if r["fundamental_rating"] is not None else None,
                    sector=r["sector"],
                    source="mode_c_chartmill_db",
                )
            )

        if self.cfg.max_basket_size is not None and self.cfg.max_basket_size > 0:
            return out[: self.cfg.max_basket_size]
        return out

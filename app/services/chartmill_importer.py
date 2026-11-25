"""ChartMill CSV Screener Importer and Ranking Engine.

Filters out earnings risks, scores momentum breakout setups,
and caps the daily active universe at 38 stocks for TradingView scanning.
"""

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
import logging

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ScreenerCandidate:
    symbol: str
    company_name: str
    price: float
    atr_pct: float
    setup_rating: float
    tech_rating: float
    relative_strength: float
    composite_score: float
    next_earnings_date: Optional[str] = None
    earnings_filtered: bool = False
    rejection_reason: Optional[str] = None
    fundamental_rating: Optional[float] = None
    growth_rating: Optional[float] = None
    health_rating: Optional[float] = None
    profitability_rating: Optional[float] = None
    valuation_rating: Optional[float] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    adr_pct: Optional[float] = None
    rs_3m: Optional[float] = None
    short_float_pct: Optional[float] = None


class ChartMillImporter:
    """Parses ChartMill CSV exports, applies earnings guards, ranks setups, and produces the top-38 basket."""

    def __init__(self) -> None:
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)

    @staticmethod
    def _normalize_header(header: str) -> str:
        """Standardizes CSV column headers for flexible parsing."""
        h = header.strip().lower()
        h = re.sub(r"[^a-z0-9]", "_", h)
        h = re.sub(r"_+", "_", h).strip("_")
        return h

    @staticmethod
    def _parse_float(val: Any, default: float = 0.0) -> float:
        if val is None:
            return default
        s = str(val).strip().replace("$", "").replace("%", "").replace(",", "")
        try:
            return float(s)
        except (ValueError, TypeError):
            return default

    def _parse_earnings_date(self, val: Any) -> Optional[date]:
        """Parses common date formats from ChartMill earnings column."""
        if not val:
            return None
        s = str(val).strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d-%b-%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    def is_earnings_risk(self, earnings_str: Any, reference_date: Optional[date] = None) -> Tuple[bool, Optional[str]]:
        """Returns True if earnings occur today or tomorrow (too risky for day trading due to halts/halts/gaps)."""
        ed = self._parse_earnings_date(earnings_str)
        if not ed:
            return False, None

        ref = reference_date or datetime.now(self.tz).date()
        tomorrow = ref + timedelta(days=1)

        if ref <= ed <= tomorrow:
            return True, f"Earnings scheduled for {ed.strftime('%Y-%m-%d')} (risk of halt/binary gap)"
        return False, None

    def calculate_composite_score(
        self,
        setup_rating: float,
        tech_rating: float,
        relative_strength: float,
        atr_pct: float,
    ) -> float:
        """
        Calculates composite momentum score:
        - Setup Rating (35%): Quality of base/consolidation near breakout
        - Technical Rating (25%): Overall trend health (Price > SMA20 > SMA50)
        - Relative Strength (25%): Market outperformance vs SPY (0-100 scale normalized to 0-10)
        - ATR% (15%): Volatility fuel for intraday expansion
        """
        score = (
            (setup_rating * 0.35)
            + (tech_rating * 0.25)
            + ((relative_strength / 10.0) * 0.25)
            + (atr_pct * 0.15)
        )
        return round(score, 3)

    def parse_csv(
        self,
        csv_content: str,
        reference_date: Optional[date] = None,
        max_basket_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Parses CSV text, filters candidates, ranks setups, and returns the qualified universe (Option B)."""
        reader = csv.DictReader(io.StringIO(csv_content))
        if not reader.fieldnames:
            return {"candidates": [], "basket": [], "basket_string": "", "total_imported": 0}

        # Map normalized header names to original
        header_map = {self._normalize_header(h): h for h in reader.fieldnames}

        # Identify key columns (supports both verbose names and ChartMill's compact headers: TA, FA, CRS, ADR(20)%, etc.)
        ticker_col = next((header_map[k] for k in header_map if k in ("ticker", "symbol")), None)
        price_col = next((header_map[k] for k in header_map if k in ("price", "close", "last", "last_price")), None)
        atr_col = next((header_map[k] for k in header_map if "atr" in k or "average_true_range" in k), None)
        adr_col = next((header_map[k] for k in header_map if "adr" in k or "average_daily_range" in k), None)
        sr_col = next((header_map[k] for k in header_map if "setup" in k), None)
        tr_col = next((header_map[k] for k in header_map if "tech" in k or k == "ta"), None)
        rs_3m_col = next((header_map[k] for k in header_map if ("3_month" in k or "3m" in k) and ("rs" in k or "crs" in k or "relative_strength" in k)), None)
        rs_col = next((header_map[k] for k in header_map if header_map[k] != rs_3m_col and ("relative_strength" in k or k in ("rs", "crs"))), None)
        name_col = next((header_map[k] for k in header_map if "name" in k or "company" in k), None)
        earnings_col = next((header_map[k] for k in header_map if "earning" in k), None)
        fund_col = next((header_map[k] for k in header_map if "fund" in k or k == "fa"), None)
        growth_col = next((header_map[k] for k in header_map if "growth" in k), None)
        health_col = next((header_map[k] for k in header_map if "health" in k), None)
        profit_col = next((header_map[k] for k in header_map if "profit" in k), None)
        val_col = next((header_map[k] for k in header_map if "val" in k and "avg" not in k), None)
        sector_col = next((header_map[k] for k in header_map if k == "sector" or "sector" in k), None)
        industry_col = next((header_map[k] for k in header_map if k == "industry" or "industry" in k), None)
        short_float_col = next((header_map[k] for k in header_map if "short" in k and ("float" in k or "interest" in k)), None)

        if not ticker_col:
            raise ValueError("CSV missing required 'Ticker' or 'Symbol' column.")

        candidates: List[ScreenerCandidate] = []
        rejected: List[ScreenerCandidate] = []

        total_rows = 0
        for row in reader:
            total_rows += 1
            symbol = row.get(ticker_col, "").strip().upper()
            if not symbol or "." in symbol or "-" in symbol:  # Skip warrants, preferreds, dot-shares
                continue

            price = self._parse_float(row.get(price_col)) if price_col else 0.0
            atr_pct = self._parse_float(row.get(atr_col)) if atr_col else 0.0
            setup_rating = self._parse_float(row.get(sr_col)) if sr_col else 0.0
            tech_rating = self._parse_float(row.get(tr_col)) if tr_col else 0.0
            relative_strength = self._parse_float(row.get(rs_col)) if rs_col else 0.0
            company_name = row.get(name_col, "").strip() if name_col else symbol
            earnings_raw = row.get(earnings_col, "") if earnings_col else None

            # Earnings check
            is_risk, reason = self.is_earnings_risk(earnings_raw, reference_date=reference_date)

            composite = self.calculate_composite_score(
                setup_rating=setup_rating,
                tech_rating=tech_rating,
                relative_strength=relative_strength,
                atr_pct=atr_pct,
            )

            cand = ScreenerCandidate(
                symbol=symbol,
                company_name=company_name,
                price=price,
                atr_pct=atr_pct,
                setup_rating=setup_rating,
                tech_rating=tech_rating,
                relative_strength=relative_strength,
                composite_score=composite,
                next_earnings_date=str(earnings_raw) if earnings_raw else None,
                earnings_filtered=is_risk,
                rejection_reason=reason,
                fundamental_rating=self._parse_float(row.get(fund_col)) if (fund_col and row.get(fund_col)) else None,
                growth_rating=self._parse_float(row.get(growth_col)) if (growth_col and row.get(growth_col)) else None,
                health_rating=self._parse_float(row.get(health_col)) if (health_col and row.get(health_col)) else None,
                profitability_rating=self._parse_float(row.get(profit_col)) if (profit_col and row.get(profit_col)) else None,
                valuation_rating=self._parse_float(row.get(val_col)) if (val_col and row.get(val_col)) else None,
                sector=row.get(sector_col, "").strip() or None if sector_col else None,
                industry=row.get(industry_col, "").strip() or None if industry_col else None,
                adr_pct=self._parse_float(row.get(adr_col)) if (adr_col and row.get(adr_col)) else None,
                rs_3m=self._parse_float(row.get(rs_3m_col)) if (rs_3m_col and row.get(rs_3m_col)) else None,
                short_float_pct=self._parse_float(row.get(short_float_col)) if (short_float_col and row.get(short_float_col)) else None,
            )

            if is_risk:
                rejected.append(cand)
            else:
                candidates.append(cand)

        # Sort valid candidates descending by composite score
        candidates.sort(key=lambda c: c.composite_score, reverse=True)

        # Dynamic Basket (Option B): all qualified candidates without artificial ceiling
        basket = candidates[:max_basket_size] if (max_basket_size is not None and max_basket_size > 0) else candidates
        basket_symbols = [c.symbol for c in basket]
        basket_string = ", ".join(basket_symbols)

        return {
            "total_imported": total_rows,
            "qualified_count": len(candidates),
            "earnings_filtered_count": len(rejected),
            "basket_size": len(basket),
            "basket": basket,
            "basket_symbols": basket_symbols,
            "basket_string": basket_string,
            "rejected": rejected,
        }


chartmill_importer = ChartMillImporter()

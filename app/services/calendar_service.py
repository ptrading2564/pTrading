"""Trading Calendar & Upcoming Events Service: NYSE Holidays, Early Closes, Macro Catalysts, and Basket Earnings."""

from __future__ import annotations

import calendar as pycal
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.brokers.base import BrokerAdapter
from app.config import settings
from app.db import get_calendar_events, get_connection

logger = logging.getLogger(__name__)

# Scheduled FOMC Rate Decision dates (14:00 ET) for 2026-2027
FOMC_DATES_2026_2027 = {
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-16",
    "2027-07-28", "2027-09-22", "2027-11-03", "2027-12-15",
}

# Scheduled US CPI Release dates (08:30 ET) for late 2026-2027
CPI_DATES_2026_2027 = {
    "2026-11-12", "2026-12-10",
    "2027-01-13", "2027-02-10", "2027-03-10", "2027-04-13",
    "2027-05-12", "2027-06-10", "2027-07-13", "2027-08-11",
    "2027-09-14", "2027-10-13", "2027-11-10", "2027-12-10",
}


def is_fomc_day(target_date: Optional[date] = None) -> bool:
    """Returns True if target_date is a scheduled FOMC Rate Decision day (14:00 ET announcement)."""
    d = target_date or datetime.now(ZoneInfo(settings.MARKET_TIMEZONE)).date()
    return d.strftime("%Y-%m-%d") in FOMC_DATES_2026_2027


def identify_us_holiday_name(d: date) -> str:
    """Maps a closed NYSE weekday date to its standard US market holiday name."""
    m, day, wd = d.month, d.day, d.weekday()
    if m == 1 and day <= 3:
        return "New Year's Day (NYSE Closed)"
    if m == 1 and wd == 0 and 15 <= day <= 21:
        return "Martin Luther King Jr. Day (NYSE Closed)"
    if m == 2 and wd == 0 and 15 <= day <= 21:
        return "Presidents' Day (NYSE Closed)"
    if m in (3, 4) and wd == 4:
        return "Good Friday (NYSE Closed)"
    if m == 5 and wd == 0 and day >= 25:
        return "Memorial Day (NYSE Closed)"
    if m == 6 and 18 <= day <= 20:
        return "Juneteenth National Independence Day (NYSE Closed)"
    if m == 7 and 3 <= day <= 5:
        return "Independence Day (NYSE Closed)"
    if m == 9 and wd == 0 and day <= 7:
        return "Labor Day (NYSE Closed)"
    if m == 11 and wd == 3 and 22 <= day <= 28:
        return "Thanksgiving Day (NYSE Closed)"
    if m == 12 and 24 <= day <= 26:
        return "Christmas Day (NYSE Closed)"
    return "NYSE Market Holiday (Closed)"


def convert_et_to_uk_time(d: date, time_et_str: Optional[str]) -> str:
    """Converts an ET HH:MM string on date d into UK (Europe/London) HH:MM GMT/BST."""
    if not time_et_str or time_et_str.upper() == "ALL_DAY":
        return "All Day"
    try:
        hh, mm = map(int, time_et_str.strip()[:5].split(":"))
        dt_et = datetime.combine(d, time(hh, mm), tzinfo=ZoneInfo(settings.MARKET_TIMEZONE))
        dt_uk = dt_et.astimezone(ZoneInfo("Europe/London"))
        return dt_uk.strftime("%H:%M %Z")
    except Exception:
        return time_et_str


def compute_macro_events(start_date: date, end_date: date) -> List[Dict[str, Any]]:
    """Generates recurring US macro & options expiration catalysts (FOMC, CPI, NFP, OpEx/Quad Witching)."""
    events: List[Dict[str, Any]] = []
    cur = start_date
    while cur <= end_date:
        d_str = cur.strftime("%Y-%m-%d")
        if d_str in FOMC_DATES_2026_2027:
            events.append(
                {
                    "date": d_str,
                    "time_et": "14:00",
                    "time_uk": convert_et_to_uk_time(cur, "14:00"),
                    "title": "FOMC Rate Decision & Press Conference",
                    "category": "MACRO",
                    "impact": "HIGH",
                    "notes": "Expect sharp 14:00 ET volatility spikes across SPY/QQQ; monitor open breakouts closely.",
                }
            )
        if d_str in CPI_DATES_2026_2027:
            events.append(
                {
                    "date": d_str,
                    "time_et": "08:30",
                    "time_uk": convert_et_to_uk_time(cur, "08:30"),
                    "title": "US CPI Inflation Report",
                    "category": "MACRO",
                    "impact": "HIGH",
                    "notes": "Pre-market 08:30 ET catalyst; expect wider 09:30-09:45 ET Opening Ranges.",
                }
            )
        # Non-Farm Payrolls: 1st Friday of the month
        if cur.weekday() == 4 and 1 <= cur.day <= 7:
            events.append(
                {
                    "date": d_str,
                    "time_et": "08:30",
                    "time_uk": convert_et_to_uk_time(cur, "08:30"),
                    "title": "US Non-Farm Payrolls (NFP Jobs Report)",
                    "category": "MACRO",
                    "impact": "HIGH",
                    "notes": "Major 08:30 ET macro release; watch opening gap volatility.",
                }
            )
        # Monthly Options Expiration / Quad Witching: 3rd Friday of the month
        if cur.weekday() == 4 and 15 <= cur.day <= 21:
            is_quad = cur.month in (3, 6, 9, 12)
            events.append(
                {
                    "date": d_str,
                    "time_et": "09:30",
                    "time_uk": convert_et_to_uk_time(cur, "09:30"),
                    "title": "Quadruple Witching Expiration" if is_quad else "Monthly Options Expiration (OpEx)",
                    "category": "MACRO",
                    "impact": "HIGH" if is_quad else "MEDIUM",
                    "notes": "Elevated volume and pinning near open/close." if is_quad else "Standard monthly options expiry.",
                }
            )
        cur += timedelta(days=1)
    return events


class CalendarService:
    """Unified calendar combining Alpaca NYSE sessions, US macro catalysts, ChartMill earnings, and custom events."""

    def __init__(self, broker: Optional[BrokerAdapter] = None) -> None:
        self.broker = broker
        self.tz = ZoneInfo(settings.MARKET_TIMEZONE)
        self._alpaca_cache: Dict[Tuple[str, str], Tuple[datetime, Dict[str, Dict[str, str]]]] = {}

    def _get_broker(self) -> BrokerAdapter:
        if self.broker is None:
            from app.brokers.alpaca_adapter import AlpacaAdapter
            self.broker = AlpacaAdapter()
        return self.broker

    async def get_trading_sessions_map(self, start_date: date, end_date: date) -> Dict[str, Dict[str, str]]:
        """Fetches & caches Alpaca official trading sessions keyed by YYYY-MM-DD."""
        key = (start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
        now = datetime.now(self.tz)
        cached = self._alpaca_cache.get(key)
        if cached and (now - cached[0]) < timedelta(hours=12):
            return cached[1]

        sessions = await self._get_broker().get_market_calendar(start_date, end_date)
        session_map = {s["date"]: s for s in sessions}
        self._alpaca_cache[key] = (now, session_map)
        return session_map

    async def is_trading_day(self, target_date: Optional[date] = None) -> bool:
        """Returns True if target_date is an official NYSE trading day."""
        d = target_date or datetime.now(self.tz).date()
        if d.weekday() >= 5:
            return False
        s_map = await self.get_trading_sessions_map(d, d)
        return d.strftime("%Y-%m-%d") in s_map

    async def get_events_in_range(self, start_date: date, end_date: date) -> List[Dict[str, Any]]:
        """Collects and sorts all NYSE holidays, early closes, macro events, basket earnings, and custom events."""
        events: List[Dict[str, Any]] = []
        s_map = await self.get_trading_sessions_map(start_date, end_date)

        # 1. NYSE Holidays & Early Closes from Alpaca official calendar
        cur = start_date
        while cur <= end_date:
            d_str = cur.strftime("%Y-%m-%d")
            if cur.weekday() < 5:
                sess = s_map.get(d_str)
                if sess is None:
                    events.append(
                        {
                            "date": d_str,
                            "time_et": "ALL_DAY",
                            "time_uk": "All Day",
                            "title": identify_us_holiday_name(cur),
                            "category": "HOLIDAY",
                            "impact": "HIGH",
                            "notes": "US equities closed. Scanner & EOD routines idle.",
                        }
                    )
                else:
                    close_str = sess.get("close", "16:00")
                    if close_str < "16:00":
                        try:
                            ch, cm = map(int, close_str.split(":"))
                            flatten_dt = datetime.combine(cur, time(ch, cm), tzinfo=self.tz) - timedelta(minutes=5)
                            flatten_et = flatten_dt.strftime("%H:%M")
                        except Exception:
                            flatten_et = settings.EARLY_CLOSE_FLATTEN_TIME
                        events.append(
                            {
                                "date": d_str,
                                "time_et": close_str,
                                "time_uk": convert_et_to_uk_time(cur, close_str),
                                "title": f"NYSE Early Close ({close_str} ET)",
                                "category": "EARLY_CLOSE",
                                "impact": "HIGH",
                                "notes": f"Adjusted EOD Flatten at {flatten_et} ET ({convert_et_to_uk_time(cur, flatten_et)}). Lighter liquidity expected.",
                            }
                        )
            cur += timedelta(days=1)

        # 2. Macro & OpEx Events
        events.extend(compute_macro_events(start_date, end_date))

        # 3. Active ChartMill Basket Earnings
        start_s = start_date.strftime("%Y-%m-%d")
        end_s = end_date.strftime("%Y-%m-%d")
        async with get_connection() as conn:
            latest_row = await (
                await conn.execute("SELECT MAX(date) as max_d FROM chartmill_universe;")
            ).fetchone()
            latest_cm_date = latest_row["max_d"] if latest_row and latest_row["max_d"] else None
            if latest_cm_date:
                rows = await (
                    await conn.execute(
                        """
                        SELECT symbol, company_name, next_earnings_date, sector
                        FROM chartmill_universe
                        WHERE date = ?
                          AND next_earnings_date >= ?
                          AND next_earnings_date <= ?
                        ORDER BY next_earnings_date ASC, symbol ASC;
                        """,
                        (latest_cm_date, start_s, end_s),
                    )
                ).fetchall()
                for r in rows:
                    ed_str = str(r["next_earnings_date"])[:10]
                    events.append(
                        {
                            "date": ed_str,
                            "time_et": "ALL_DAY",
                            "time_uk": "Earnings",
                            "title": f"{r['symbol']} Earnings ({r['company_name'] or r['symbol']})",
                            "category": "EARNINGS",
                            "impact": "MEDIUM",
                            "symbol": r["symbol"],
                            "notes": f"Active basket stock ({r['sector'] or 'US Equity'}) — auto-filtered by earnings guard within 24h.",
                        }
                    )

        # 4. Custom User Events
        custom_rows = await get_calendar_events(start_s, end_s)
        for cr in custom_rows:
            ed_date = datetime.strptime(cr["event_date"], "%Y-%m-%d").date()
            events.append(
                {
                    "id": cr["id"],
                    "date": cr["event_date"],
                    "time_et": cr.get("event_time_et") or "ALL_DAY",
                    "time_uk": convert_et_to_uk_time(ed_date, cr.get("event_time_et")),
                    "title": cr["title"],
                    "category": cr.get("category") or "CUSTOM",
                    "impact": cr.get("impact") or "MEDIUM",
                    "notes": cr.get("notes") or "",
                    "is_custom": True,
                }
            )

        events.sort(key=lambda e: (e["date"], e.get("time_et", "99:99"), e["title"]))
        return events

    async def get_upcoming_alerts(self) -> List[Dict[str, Any]]:
        """Returns high-priority alerts for today and tomorrow (Holidays, Early Closes, High-Impact Macro)."""
        today = datetime.now(self.tz).date()
        tomorrow = today + timedelta(days=1)
        events = await self.get_events_in_range(today, tomorrow)
        alerts = []
        for e in events:
            if e["category"] in ("HOLIDAY", "EARLY_CLOSE") or (e["category"] == "MACRO" and e["impact"] == "HIGH"):
                e_copy = dict(e)
                e_copy["when_label"] = "TODAY" if e["date"] == today.strftime("%Y-%m-%d") else "TOMORROW"
                alerts.append(e_copy)
        return alerts

    async def get_month_view(self, year: Optional[int] = None, month: Optional[int] = None) -> Dict[str, Any]:
        """Builds the monthly calendar grid, past daily P&L overlay, summary cards, and 60-day upcoming feed."""
        now_et = datetime.now(self.tz)
        today = now_et.date()
        yr = year or today.year
        mo = month or today.month
        if mo < 1 or mo > 12:
            yr, mo = today.year, today.month

        first_day = date(yr, mo, 1)
        last_day = date(yr, mo, pycal.monthrange(yr, mo)[1])

        month_events = await self.get_events_in_range(first_day, last_day)
        events_by_day: Dict[str, List[Dict[str, Any]]] = {}
        for ev in month_events:
            events_by_day.setdefault(ev["date"], []).append(ev)

        # Fetch daily_stats P&L overlay for this month
        pnl_by_day: Dict[str, Dict[str, Any]] = {}
        async with get_connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT date, starting_equity, current_equity, realized_pnl, trade_count
                    FROM daily_stats
                    WHERE date >= ? AND date <= ?;
                    """,
                    (first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d")),
                )
            ).fetchall()
            for r in rows:
                s_e = float(r["starting_equity"])
                c_e = float(r["current_equity"])
                roi_pct = round(((c_e - s_e) / s_e) * 100.0, 2) if s_e > 0 else 0.0
                pnl_by_day[r["date"]] = {
                    "pnl": round(c_e - s_e, 2),
                    "roi_pct": roi_pct,
                    "trade_count": int(r["trade_count"] or 0),
                }

        # Build calendar weeks (Monday-Sunday)
        cal = pycal.Calendar(firstweekday=0)
        weeks: List[List[Dict[str, Any]]] = []
        for week in cal.monthdatescalendar(yr, mo):
            week_cells = []
            for d in week:
                d_str = d.strftime("%Y-%m-%d")
                week_cells.append(
                    {
                        "date": d_str,
                        "day": d.day,
                        "in_month": d.month == mo,
                        "is_today": d == today,
                        "is_weekend": d.weekday() >= 5,
                        "events": events_by_day.get(d_str, []),
                        "pnl_info": pnl_by_day.get(d_str),
                    }
                )
            weeks.append(week_cells)

        # Upcoming 60-day feed & summary highlights
        upcoming = await self.get_events_in_range(today, today + timedelta(days=60))
        next_holiday = next((e for e in upcoming if e["category"] == "HOLIDAY"), None)
        next_early_close = next((e for e in upcoming if e["category"] == "EARLY_CLOSE"), None)
        next_macro = next((e for e in upcoming if e["category"] == "MACRO" and e["impact"] == "HIGH"), None)
        earnings_14d = [
            e for e in upcoming
            if e["category"] == "EARNINGS" and e["date"] <= (today + timedelta(days=14)).strftime("%Y-%m-%d")
        ]

        prev_m = date(yr - 1, 12, 1) if mo == 1 else date(yr, mo - 1, 1)
        next_m = date(yr + 1, 1, 1) if mo == 12 else date(yr, mo + 1, 1)

        return {
            "year": yr,
            "month": mo,
            "month_name": first_day.strftime("%B %Y"),
            "prev_year": prev_m.year,
            "prev_month": prev_m.month,
            "next_year": next_m.year,
            "next_month": next_m.month,
            "today_str": today.strftime("%Y-%m-%d"),
            "weeks": weeks,
            "upcoming_events": upcoming,
            "next_holiday": next_holiday,
            "next_early_close": next_early_close,
            "next_macro": next_macro,
            "earnings_14d_count": len(earnings_14d),
        }


calendar_service = CalendarService()

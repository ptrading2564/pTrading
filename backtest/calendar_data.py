"""Historical & scheduled US FOMC dates and fallback NYSE trading calendar helpers."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Set

from app.services.calendar_service import FOMC_DATES_2026_2027

# Official Federal Reserve FOMC Rate Decision announcement dates (14:00 ET) for 2020-2025
FOMC_DATES_HISTORICAL: Set[str] = {
    # 2020
    "2020-01-29", "2020-03-03", "2020-03-15", "2020-04-29", "2020-06-10",
    "2020-07-29", "2020-09-16", "2020-11-05", "2020-12-16",
    # 2021
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16",
    "2021-07-28", "2021-09-22", "2021-11-03", "2021-12-15",
    # 2022
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15",
    "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    # 2023
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14",
    "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    # 2024
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
}

ALL_FOMC_DATES: Set[str] = FOMC_DATES_HISTORICAL | set(FOMC_DATES_2026_2027)

# Known NYSE 13:00 ET early closes (Day after Thanksgiving, Christmas Eve, July 3rd when weekday)
KNOWN_EARLY_CLOSES: Set[str] = {
    "2022-11-25",
    "2023-07-03", "2023-11-24",
    "2024-07-03", "2024-11-29", "2024-12-24",
    "2025-07-03", "2025-11-28", "2025-12-24",
    "2026-11-27", "2026-12-24",
    "2027-11-26",
}


def is_fomc_day_historical(target_date: date) -> bool:
    """Returns True if target_date is a scheduled FOMC decision day (2020-2027)."""
    return target_date.strftime("%Y-%m-%d") in ALL_FOMC_DATES


def generate_fallback_sessions(start_date: date, end_date: date) -> Dict[str, Dict[str, str]]:
    """Offline weekday trading session map used only when Alpaca calendar API is unavailable."""
    sessions: Dict[str, Dict[str, str]] = {}
    cur = start_date
    while cur <= end_date:
        if cur.weekday() < 5:
            d_str = cur.strftime("%Y-%m-%d")
            close_str = "13:00" if d_str in KNOWN_EARLY_CLOSES else "16:00"
            sessions[d_str] = {"date": d_str, "open": "09:30", "close": close_str}
        cur += timedelta(days=1)
    return sessions

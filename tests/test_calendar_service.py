"""Tests for CalendarService (NYSE holidays, early closes, macro events, ET->UK conversion) and /calendar routes."""

from datetime import date
import httpx
import pytest

from app.config import settings
from app.db import add_calendar_event, init_db
from app.main import app
from app.services.calendar_service import (
    CalendarService,
    compute_macro_events,
    convert_et_to_uk_time,
    identify_us_holiday_name,
)
from tests.test_risk_manager import MockBroker


class HolidayMockBroker(MockBroker):
    """Simulates Thanksgiving (2026-11-26 closed) and Black Friday (2026-11-27 early close 13:00)."""

    async def get_market_calendar(self, start_date: date, end_date: date):
        return [
            {"date": "2026-11-25", "open": "09:30", "close": "16:00"},
            # 2026-11-26 omitted -> Thanksgiving Holiday
            {"date": "2026-11-27", "open": "09:30", "close": "13:00"},
        ]


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()


def test_holiday_names_and_uk_time_conversion():
    assert "Thanksgiving" in identify_us_holiday_name(date(2026, 11, 26))
    assert "Christmas" in identify_us_holiday_name(date(2026, 12, 25))
    # Nov 27 is EST (UTC-5) and UK is GMT (UTC+0), so 13:00 ET == 18:00 GMT
    uk = convert_et_to_uk_time(date(2026, 11, 27), "13:00")
    assert uk.startswith("18:00")


def test_macro_events_generation():
    events = compute_macro_events(date(2026, 12, 1), date(2026, 12, 31))
    titles = [e["title"] for e in events]
    assert any("FOMC" in t for t in titles)
    assert any("CPI" in t for t in titles)
    assert any("Non-Farm Payrolls" in t for t in titles)
    assert any("Quadruple Witching" in t for t in titles)


@pytest.mark.asyncio
async def test_calendar_service_detects_holiday_and_early_close():
    svc = CalendarService(broker=HolidayMockBroker())
    assert await svc.is_trading_day(date(2026, 11, 25)) is True
    assert await svc.is_trading_day(date(2026, 11, 26)) is False

    events = await svc.get_events_in_range(date(2026, 11, 25), date(2026, 11, 27))
    cats = {(e["date"], e["category"]) for e in events}
    assert ("2026-11-26", "HOLIDAY") in cats
    assert ("2026-11-27", "EARLY_CLOSE") in cats

    early = next(e for e in events if e["category"] == "EARLY_CLOSE")
    assert "12:55 ET" in early["notes"]


@pytest.mark.asyncio
async def test_calendar_routes_and_custom_events():
    auth = (settings.DASHBOARD_USERNAME, settings.DASHBOARD_PASSWORD)
    await add_calendar_event("2026-12-15", "Fed Chair Speech", "10:00", "CUSTOM", "HIGH", "Watch QQQ")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res_html = await client.get("/calendar?year=2026&month=12", auth=auth)
        assert res_html.status_code == 200
        assert "Market Calendar" in res_html.text
        assert "Fed Chair Speech" in res_html.text

        res_api = await client.get("/api/calendar/events?days=30", auth=auth)
        assert res_api.status_code == 200
        assert "events" in res_api.json()

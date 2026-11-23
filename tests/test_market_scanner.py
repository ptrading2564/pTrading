"""Unit tests for Server-Side MarketScanner (Option 3)."""

from datetime import datetime, time, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from zoneinfo import ZoneInfo

from app.config import settings
from app.db import init_db, set_system_state
from app.services.market_scanner import MarketScanner
from tests.test_risk_manager import MockBroker


class MockBar:
    def __init__(self, timestamp, open_p, high_p, low_p, close_p, volume):
        self.timestamp = timestamp
        self.open = open_p
        self.high = high_p
        self.low = low_p
        self.close = close_p
        self.volume = volume


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()
    await set_system_state("kill_switch", "OFF")
    await set_system_state("execution_mode", "AUTO")


@pytest.mark.asyncio
async def test_market_scanner_calculations():
    mock_broker = MockBroker()
    mock_risk = MagicMock()
    scanner = MarketScanner(mock_broker, mock_risk)

    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    base_t = datetime(2026, 11, 23, 10, 0, tzinfo=tz)

    bars = [
        MockBar(base_t, 100.0, 102.0, 99.0, 101.0, 1000.0),
        MockBar(base_t, 101.0, 103.0, 100.5, 102.5, 2000.0),
        MockBar(base_t, 102.5, 104.0, 102.0, 103.5, 1500.0),
    ]

    # Test VWAP
    # Bar 1: TP = (102+99+101)/3 = 100.6667, Vol = 1000, TP*V = 100666.67
    # Bar 2: TP = (103+100.5+102.5)/3 = 102.0, Vol = 2000, TP*V = 204000
    # Bar 3: TP = (104+102+103.5)/3 = 103.1667, Vol = 1500, TP*V = 154750
    # Total TP*V = 459416.67 / 4500 = 102.09
    vwap = scanner.calculate_intraday_vwap(bars)
    assert 102.0 < vwap < 102.2

    # Test ATR
    atr = scanner.calculate_atr(bars, period=14)
    assert atr > 0.0

    # Test RVOL: Bar 3 volume 1500 vs avg of first 2 (1000+2000)/2 = 1500 -> 1.0
    rvol = scanner.calculate_rvol(bars, period=20)
    assert abs(rvol - 1.0) < 0.01


@pytest.mark.asyncio
async def test_market_scanner_basket_fallback():
    mock_broker = MockBroker()
    mock_risk = MagicMock()
    scanner = MarketScanner(mock_broker, mock_risk)

    # Empty DB state -> falls back to settings.DEFAULT_BASKET
    basket = await scanner.get_active_basket()
    assert len(basket) >= 30
    assert "NVDA" in basket
    assert "TSLA" in basket

    # Custom DB state
    await set_system_state("latest_chartmill_basket", "AAPL,MSFT,GOOGL")
    custom_basket = await scanner.get_active_basket()
    assert custom_basket == ["AAPL", "MSFT", "GOOGL"]


@pytest.mark.asyncio
async def test_market_scanner_closed_market():
    mock_broker = MockBroker()
    mock_risk = MagicMock()
    scanner = MarketScanner(mock_broker, mock_risk)

    # Mock datetime to evening (20:00 ET)
    tz = ZoneInfo(settings.MARKET_TIMEZONE)
    fake_now = datetime(2026, 11, 23, 20, 0, tzinfo=tz)

    with patch("app.services.market_scanner.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.combine = datetime.combine
        mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        results = await scanner.scan_once()
        assert results == []
        assert "MARKET_CLOSED" in scanner.status_message

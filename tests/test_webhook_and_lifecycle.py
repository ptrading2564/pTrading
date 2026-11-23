"""Tests for Webhook ingestion, security, idempotency, and Lifecycle worker."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import httpx
import pytest
from app.brokers.base import OrderInfo, PositionInfo
from app.config import settings
from app.db import get_connection, init_db, set_system_state
from app.main import app
from app.services.lifecycle_worker import LifecycleWorker


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()
    await set_system_state("kill_switch", "OFF")
    await set_system_state("execution_mode", "AUTO")


@pytest.mark.asyncio
async def test_webhook_unauthorized_secret():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # No secret provided
        res = await client.post(
            "/webhook/tradingview",
            json={"symbol": "AAPL", "close": 150.0, "or_high": 149.0, "or_low": 148.0, "atr": 1.0},
        )
        assert res.status_code == 401

        # Invalid secret
        res = await client.post(
            "/webhook/tradingview",
            json={"symbol": "AAPL", "close": 150.0, "or_high": 149.0, "or_low": 148.0, "atr": 1.0, "secret": "wrong_secret"},
        )
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_webhook_ip_allowlist_check():
    # Test unauthorized IP
    with patch.object(settings, "VERIFY_TRADINGVIEW_IP", True):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post(
                "/webhook/tradingview",
                headers={"X-Forwarded-For": "192.168.1.100"},
                json={"symbol": "AAPL", "close": 150.0, "or_high": 149.0, "or_low": 148.0, "atr": 1.0, "secret": settings.WEBHOOK_SECRET},
            )
            assert res.status_code == 403


@pytest.mark.asyncio
async def test_webhook_valid_and_idempotency():
    with patch.object(settings, "VERIFY_TRADINGVIEW_IP", False):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            payload = {
                "symbol": "AAPL",
                "close": 150.0,
                "or_high": 149.0,
                "or_low": 148.0,
                "atr": 1.0,
                "bar_time": 1700000000,
                "secret": settings.WEBHOOK_SECRET,
            }

            # First post: Accepted & recorded
            res1 = await client.post("/webhook/tradingview", json=payload)
            assert res1.status_code == 200
            data1 = res1.json()
            assert data1["status"] == "success"

            # Second post with identical bar_time and symbol: Duplicated & ignored
            res2 = await client.post("/webhook/tradingview", json=payload)
            assert res2.status_code == 200
            data2 = res2.json()
            assert data2["results"][0]["status"] == "duplicate"


@pytest.mark.asyncio
async def test_webhook_semi_auto_mode():
    await set_system_state("execution_mode", "SEMI_AUTO")

    with patch.object(settings, "VERIFY_TRADINGVIEW_IP", False):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            payload = {
                "symbol": "NVDA",
                "close": 140.0,
                "or_high": 139.0,
                "or_low": 137.0,
                "atr": 1.5,
                "bar_time": 1700000005,
                "secret": settings.WEBHOOK_SECRET,
            }

            res = await client.post("/webhook/tradingview", json=payload)
            assert res.status_code == 200
            data = res.json()
            assert data["results"][0]["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_lifecycle_breakeven_step():
    from tests.test_risk_manager import MockBroker
    mock_broker = MockBroker()
    worker = LifecycleWorker(mock_broker)

    # Insert an open trade into SQLite: Entry=100.0, Stop=98.0 (1R=2.0, BE Target = 102.0)
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO trades (client_order_id, symbol, qty, entry_price, stop_loss_price, take_profit_price, is_open, breakeven_stepped)
            VALUES ('pt_test_1', 'MSFT', 100, 100.0, 98.0, 104.0, 1, 0);
            """
        )
        await conn.commit()

    # Position reached 102.50 (+1.25R, above +1.0R target)
    mock_broker.positions = [
        PositionInfo("MSFT", 100, 100.0, 102.50, 250.0, 10250.0)
    ]
    # Active stop order exists
    mock_broker.orders = [
        OrderInfo("stop_123", "c_stop_123", "MSFT", "new", "sell", 100, 0, None, 98.0, datetime.now(timezone.utc), None)
    ]

    mock_broker.replace_stop_loss = AsyncMock(return_value=True)

    await worker.step_breakeven_if_earned()

    # Verify broker replace_stop_loss was called with Entry + $0.02 (100.02)
    mock_broker.replace_stop_loss.assert_called_once_with("stop_123", 100.02)

    # Verify DB was updated
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT breakeven_stepped, stop_loss_price FROM trades WHERE symbol = 'MSFT';")
        row = await cursor.fetchone()
        assert row["breakeven_stepped"] == 1
        assert row["stop_loss_price"] == 100.02


@pytest.mark.asyncio
async def test_lifecycle_cancel_stale_orders():
    from tests.test_risk_manager import MockBroker
    mock_broker = MockBroker()
    worker = LifecycleWorker(mock_broker)

    old_time = datetime.now(timezone.utc) - timedelta(seconds=200)  # > 180 seconds stale
    mock_broker.orders = [
        OrderInfo("order_stale", "c_stale", "OLD", "new", "buy", 50, 0, 50.0, None, old_time, None)
    ]
    mock_broker.cancel_order = AsyncMock(return_value=True)

    await worker.cancel_stale_orders()

    mock_broker.cancel_order.assert_called_once_with("order_stale")

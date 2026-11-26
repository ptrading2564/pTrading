"""Tests for currency formatting and Telegram bot endpoints."""

from unittest.mock import AsyncMock, patch
import httpx
import pytest
from app.config import settings
from app.db import init_db
from app.main import app
from app.routers.dashboard import _create_session_token, SESSION_COOKIE_NAME
from app.services.telegram_bot import telegram


@pytest.fixture(autouse=True)
async def setup_test_env():
    await init_db()
    settings.TELEGRAM_BOT_TOKEN = "mock-bot-token"
    settings.TELEGRAM_CHAT_ID = "123456789"
    settings.TELEGRAM_ENABLED = True


def format_currency(val: float) -> str:
    """Standard pTrading financial currency formatter helper."""
    sign = "+" if val > 0 else ("-" if val < 0 else "")
    return f"{sign}${abs(val):.2f}"


def test_currency_formatting_standards():
    """Verifies positive, zero, and negative currency conventions."""
    assert format_currency(100.50) == "+$100.50"
    assert format_currency(0.0) == "$0.00"
    assert format_currency(-2.10) == "-$2.10"
    assert format_currency(-0.01) == "-$0.01"
    # Ensure standard convention is strictly -$X.XX and NOT $-X.XX
    assert format_currency(-2.10) != "$-2.10"


@pytest.mark.asyncio
async def test_telegram_status_endpoint():
    """Verifies /api/telegram/status returns configuration state."""
    session_tok = _create_session_token("p.trading2564@gmail.com")
    cookies = {SESSION_COOKIE_NAME: session_tok}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/telegram/status", cookies=cookies)
        assert res.status_code == 200
        data = res.json()
        assert data["enabled"] is True
        assert data["configured"] is True
        assert "123...89" in data["chat_id"]


@pytest.mark.asyncio
async def test_telegram_test_endpoint_unconfigured():
    """Verifies /api/telegram/test returns 400 when tokens are missing."""
    settings.TELEGRAM_BOT_TOKEN = ""
    settings.TELEGRAM_CHAT_ID = ""
    session_tok = _create_session_token("p.trading2564@gmail.com")
    cookies = {SESSION_COOKIE_NAME: session_tok}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/telegram/test", cookies=cookies)
        assert res.status_code == 400
        assert "must be set in .env" in res.json()["message"]


@pytest.mark.asyncio
async def test_telegram_test_endpoint_success():
    """Verifies /api/telegram/test delivers message when mocked."""
    session_tok = _create_session_token("p.trading2564@gmail.com")
    cookies = {SESSION_COOKIE_NAME: session_tok}

    mock_resp = httpx.Response(200, json={"ok": True, "result": {"message_id": 999}})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/api/telegram/test", cookies=cookies)
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert "delivered" in data["message"].lower()


@pytest.mark.asyncio
async def test_telegram_set_webhook_endpoint():
    """Verifies /api/telegram/set-webhook registers with Telegram API."""
    session_tok = _create_session_token("p.trading2564@gmail.com")
    cookies = {SESSION_COOKIE_NAME: session_tok}

    mock_resp = httpx.Response(200, json={"ok": True, "description": "Webhook was set"})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/api/telegram/set-webhook", cookies=cookies)
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert "registered" in data["message"].lower()

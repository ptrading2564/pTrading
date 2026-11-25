"""Tests for Google OAuth 2.0 Single Sign-On and In-App Fallback Password Management."""

from unittest.mock import AsyncMock, patch
import httpx
import pytest
from app.config import settings
from app.db import get_system_state, init_db, set_system_state
from app.main import app
from app.routers.dashboard import (
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE_NAME,
    _check_dashboard_password,
    _create_session_token,
    _hash_password,
    _verify_password_hash,
    _verify_session_token,
)


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()
    # Reset any custom password hash
    await set_system_state("dashboard_password_hash", "")
    settings.GOOGLE_CLIENT_ID = "mock-google-client-id.apps.googleusercontent.com"
    settings.GOOGLE_CLIENT_SECRET = "mock-google-client-secret"
    settings.ALLOWED_GOOGLE_EMAILS = "p.trading2564@gmail.com"
    settings.GOOGLE_OAUTH_ENABLED = True


@pytest.mark.asyncio
async def test_session_token_and_password_hashing():
    """Verifies token creation, verification, and PBKDF2 password hashing."""
    # 1. Plain username session token
    tok1 = _create_session_token("p.trading2564@gmail.com")
    assert tok1.startswith("v2.")
    v1 = _verify_session_token(tok1)
    assert v1 is not None
    assert v1["user"] == "p.trading2564@gmail.com"

    # 2. Rich dict session token (with name & avatar)
    tok2 = _create_session_token({
        "user": "p.trading2564@gmail.com",
        "name": "Peter Trader",
        "avatar": "https://lh3.googleusercontent.com/avatar123",
    })
    v2 = _verify_session_token(tok2)
    assert v2 is not None
    assert v2["user"] == "p.trading2564@gmail.com"
    assert v2["name"] == "Peter Trader"
    assert v2["avatar"] == "https://lh3.googleusercontent.com/avatar123"

    # 3. Unauthorized email token
    tok_bad = _create_session_token("stranger@gmail.com")
    assert _verify_session_token(tok_bad) is None

    # 4. PBKDF2 Password hashing
    hashed = _hash_password("MyNewPassword123!")
    assert _verify_password_hash("MyNewPassword123!", hashed) is True
    assert _verify_password_hash("WrongPassword", hashed) is False


@pytest.mark.asyncio
async def test_google_login_redirect_and_state():
    """Verifies /auth/google/login generates valid state cookie and Google redirect URL."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/auth/google/login?next=/analytics", follow_redirects=False)
        assert res.status_code == 303
        location = res.headers.get("location", "")
        assert "accounts.google.com/o/oauth2/v2/auth" in location
        assert "client_id=mock-google-client-id" in location
        assert "scope=openid+email+profile" in location
        assert OAUTH_STATE_COOKIE in res.cookies


@pytest.mark.asyncio
async def test_google_callback_state_mismatch_rejected():
    """Verifies callback fails securely when CSRF state does not match cookie."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Missing state cookie
        res = await client.get("/auth/google/callback?code=mock_code&state=fake_state", follow_redirects=False)
        assert res.status_code == 303
        assert "error=state_mismatch" in res.headers.get("location", "")

        # 2. Tampered state cookie
        tampered_cookies = {OAUTH_STATE_COOKIE: "bad_state:/:fake_signature"}
        res2 = await client.get(
            "/auth/google/callback?code=mock_code&state=fake_state",
            cookies=tampered_cookies,
            follow_redirects=False,
        )
        assert res2.status_code == 303
        assert "error=state_mismatch" in res2.headers.get("location", "")


@pytest.mark.asyncio
async def test_google_callback_authorized_email():
    """Verifies authorized Google email is issued a session cookie and redirected."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # First initiate login to get signed state cookie
        login_res = await client.get("/auth/google/login?next=/analytics", follow_redirects=False)
        state_cookie = login_res.cookies[OAUTH_STATE_COOKIE]
        raw_state = state_cookie.split(":")[0]

        # Mock Google Token & Userinfo APIs
        mock_token_resp = httpx.Response(200, json={"access_token": "mock-access-token-xyz"})
        mock_userinfo_resp = httpx.Response(
            200,
            json={
                "email": "p.trading2564@gmail.com",
                "name": "Peter Trader",
                "picture": "https://lh3.googleusercontent.com/photo.jpg",
            },
        )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_token_resp), \
             patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_userinfo_resp):
            cb_res = await client.get(
                f"/auth/google/callback?code=valid_auth_code&state={raw_state}",
                cookies={OAUTH_STATE_COOKIE: state_cookie},
                follow_redirects=False,
            )
            assert cb_res.status_code == 303
            assert cb_res.headers.get("location") == "/analytics"
            assert SESSION_COOKIE_NAME in cb_res.cookies

            # Verify session works
            session_tok = cb_res.cookies[SESSION_COOKIE_NAME]
            verified = _verify_session_token(session_tok)
            assert verified["user"] == "p.trading2564@gmail.com"
            assert verified["name"] == "Peter Trader"


@pytest.mark.asyncio
async def test_google_callback_unauthorized_email_rejected():
    """Verifies non-allowlisted Google emails are blocked."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login_res = await client.get("/auth/google/login", follow_redirects=False)
        state_cookie = login_res.cookies[OAUTH_STATE_COOKIE]
        raw_state = state_cookie.split(":")[0]

        mock_token_resp = httpx.Response(200, json={"access_token": "mock-access-token-xyz"})
        mock_userinfo_resp = httpx.Response(
            200,
            json={
                "email": "unauthorized_user@gmail.com",
                "name": "Intruder",
                "picture": "",
            },
        )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_token_resp), \
             patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_userinfo_resp):
            cb_res = await client.get(
                f"/auth/google/callback?code=valid_auth_code&state={raw_state}",
                cookies={OAUTH_STATE_COOKIE: state_cookie},
                follow_redirects=False,
            )
            assert cb_res.status_code == 303
            assert "error=unauthorized_email" in cb_res.headers.get("location", "")
            assert SESSION_COOKIE_NAME not in cb_res.cookies


@pytest.mark.asyncio
async def test_in_app_password_change_flow():
    """Verifies changing password in-app updates SQLite and enables subsequent login."""
    session_tok = _create_session_token("p.trading2564@gmail.com")
    cookies = {SESSION_COOKIE_NAME: session_tok}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Password mismatch
        res_mismatch = await client.post(
            "/api/auth/change-password",
            data={
                "current_password": settings.DASHBOARD_PASSWORD,
                "new_password": "NewSecretPass123!",
                "confirm_password": "DifferentSecretPass123!",
            },
            cookies=cookies,
        )
        assert res_mismatch.status_code == 400

        # 2. Incorrect current password
        res_wrong_current = await client.post(
            "/api/auth/change-password",
            data={
                "current_password": "WrongCurrentPassword",
                "new_password": "NewSecretPass123!",
                "confirm_password": "NewSecretPass123!",
            },
            cookies=cookies,
        )
        assert res_wrong_current.status_code == 403

        # 3. Successful password change
        res_success = await client.post(
            "/api/auth/change-password",
            data={
                "current_password": settings.DASHBOARD_PASSWORD,
                "new_password": "BrandNewTradingPassword2026!",
                "confirm_password": "BrandNewTradingPassword2026!",
            },
            cookies=cookies,
        )
        assert res_success.status_code == 200
        assert res_success.json()["status"] == "success"

        # 4. Old password should now FAIL
        login_old = await client.post(
            "/login",
            data={
                "username": "p.trading2564@gmail.com",
                "password": settings.DASHBOARD_PASSWORD,
            },
            follow_redirects=False,
        )
        assert login_old.status_code == 401

        # 5. New password should now SUCCEED
        login_new = await client.post(
            "/login",
            data={
                "username": "p.trading2564@gmail.com",
                "password": "BrandNewTradingPassword2026!",
            },
            follow_redirects=False,
        )
        assert login_new.status_code == 303
        assert SESSION_COOKIE_NAME in login_new.cookies

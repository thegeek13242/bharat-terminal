"""
Authentication for the Bharat Terminal API — Google OAuth + invite allowlist.

Flow:
  1. Frontend obtains a Google ID token via Google Identity Services.
  2. POST /auth/google { id_token } -> we verify the token against GOOGLE_CLIENT_ID,
     require email_verified, then check the email against the app_users allowlist
     (a row there == an invitation). Unknown/inactive email -> 403 not_invited.
  3. On success we mint our own short-ish-lived session JWT (HS256, APP_JWT_SECRET)
     and set it as an httpOnly, Secure, SameSite=Lax cookie. The app is served
     same-origin (nginx proxies /api), so the cookie is first-party and CSRF-safe
     under SameSite=Lax.

`get_current_user` is the dependency that guards the data routes: it reads the
session cookie, verifies it, and returns the {id, email} claims (401 otherwise).

Secrets (never committed; see .env.example):
  GOOGLE_CLIENT_ID  — the OAuth Web client id (also used by the frontend).
  APP_JWT_SECRET    — HS256 signing key for our session cookie.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import text

from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
APP_JWT_SECRET = os.getenv("APP_JWT_SECRET", "")
JWT_ALG = "HS256"

SESSION_COOKIE = "tr_session"
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "7"))
# Cookie is Secure by default; override to "false" only for plain-HTTP local dev.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() not in ("false", "0", "no")


# ── Session token (our own JWT) ────────────────────────────────────────────
def create_session_token(user_id: int | str, email: str) -> str:
    if not APP_JWT_SECRET:
        raise HTTPException(status_code=500, detail="auth_not_configured")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=SESSION_TTL_DAYS)).timestamp()),
    }
    return jwt.encode(payload, APP_JWT_SECRET, algorithm=JWT_ALG)


def decode_session_token(token: str) -> dict:
    return jwt.decode(token, APP_JWT_SECRET, algorithms=[JWT_ALG])


# ── Google ID-token verification ───────────────────────────────────────────
def _verify_google_sync(id_token_str: str) -> dict:
    """Blocking verification against Google's certs; run in a threadpool."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="auth_not_configured")
    return google_id_token.verify_oauth2_token(
        id_token_str, google_requests.Request(), GOOGLE_CLIENT_ID
    )


async def verify_google_id_token(id_token_str: str) -> dict:
    try:
        idinfo = await run_in_threadpool(_verify_google_sync, id_token_str)
    except HTTPException:
        raise
    except Exception as e:  # ValueError on bad/expired token, etc.
        logger.info("Google ID token verification failed: %s", e)
        raise HTTPException(status_code=401, detail="invalid_google_token")
    if not idinfo.get("email_verified"):
        raise HTTPException(status_code=401, detail="email_not_verified")
    return idinfo


# ── Allowlist lookup / upsert ──────────────────────────────────────────────
async def get_active_user_by_email(email: str) -> Optional[dict]:
    factory = get_session_factory()
    async with factory() as s:
        row = (await s.execute(
            text(
                "SELECT id, email, is_active, name, picture "
                "FROM app_users WHERE lower(email) = lower(:email)"
            ),
            {"email": email},
        )).mappings().first()
    return dict(row) if row else None


async def record_login(user_id: int, idinfo: dict) -> None:
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            text(
                "UPDATE app_users SET last_login_at = now(), "
                "google_sub = :sub, name = COALESCE(:name, name), "
                "picture = COALESCE(:picture, picture) WHERE id = :id"
            ),
            {
                "id": user_id,
                "sub": idinfo.get("sub"),
                "name": idinfo.get("name"),
                "picture": idinfo.get("picture"),
            },
        )
        await s.commit()


# ── FastAPI dependency: require an authenticated user ──────────────────────
async def get_current_user(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not_authenticated"
        )
    if not APP_JWT_SECRET:
        raise HTTPException(status_code=500, detail="auth_not_configured")
    try:
        payload = decode_session_token(token)
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_session"
        )
    return {"id": payload.get("sub"), "email": payload.get("email")}


def user_from_session_cookie(cookies: dict) -> Optional[dict]:
    """Non-raising variant for the WebSocket handshake."""
    token = cookies.get(SESSION_COOKIE)
    if not token or not APP_JWT_SECRET:
        return None
    try:
        payload = decode_session_token(token)
    except jwt.PyJWTError:
        return None
    return {"id": payload.get("sub"), "email": payload.get("email")}

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

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import redis.asyncio as aioredis
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


# ── Internal research allowlist ─────────────────────────────────────────────
# Comma-separated emails allowed to hit internal research surfaces (signal
# reports, accuracy metrics). Empty (default) = nobody: those endpoints are
# never customer-facing — recommendation content must not reach client
# accounts (SEBI RA scope).
INTERNAL_RESEARCH_EMAILS = {
    e.strip().lower()
    for e in os.getenv("INTERNAL_RESEARCH_EMAILS", "").split(",")
    if e.strip()
}

# Bootstrap/lockout fallback for the admin panel. Roles live in app_users.role
# (migration 035); this env set exists so the founder can never be locked out
# by a bad DB state and so the panel works before the migration lands.
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "").split(",")
    if e.strip()
}


# ── Per-user feature flags ──────────────────────────────────────────────────
# Single source of truth for what the admin panel can toggle per user. Keys map
# to router prefixes; enforcement is require_feature() on the router (main.py),
# nav filtering is the frontend reading disabled_features off /auth/me.
# Absence of a user_features row = enabled. Admin role bypasses all gates.
FEATURE_REGISTRY: dict[str, list[str]] = {
    "watchlist": ["/watchlist"],
    "research_chat": ["/research-chat"],
    "papertrading": ["/papertrading"],
    "transcripts": ["/transcripts"],
    "investors": ["/investors", "/deals"],
    "surprises": ["/earnings-surprises"],
    "dossiers": ["/dossier"],
    "db_explorer": ["/db"],
    "signals": ["/signals", "/signal-accuracy"],
    "sentiment": ["/sentiment"],
    "ltp": ["/internal/ltp"],
}


# ── DB-backed authorization (role + revocation + feature overrides) ─────────
# The JWT alone was previously the whole session: deactivating a user left a
# valid 7-day cookie working everywhere. load_authz closes that: every request
# re-checks app_users through a 60s Redis cache; admin writes delete the cache
# key so revocation/role changes are effectively instant.
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
AUTHZ_CACHE_TTL_S = int(os.getenv("AUTHZ_CACHE_TTL_S", "60"))
_redis = None


async def _get_redis():
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def _authz_key(user_id: str) -> str:
    return f"authz:user:{user_id}"


async def invalidate_authz(user_id: int | str) -> None:
    """Delete the cached authz state; next request re-reads the DB."""
    try:
        r = await _get_redis()
        await r.delete(_authz_key(str(user_id)))
    except Exception as e:
        logger.warning("authz cache invalidation failed for %s: %s", user_id, e)


async def _load_authz_db(user_id: str) -> Optional[dict]:
    """One DB round for {role, is_active, disabled_features}. None = no row."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    factory = get_session_factory()
    async with factory() as s:
        row = (await s.execute(
            text("SELECT role, is_active FROM app_users WHERE id = :id"),
            {"id": uid},
        )).mappings().first()
        if row is None:
            return None
        disabled: list[str] = []
        try:
            disabled = list((await s.execute(
                text(
                    "SELECT feature FROM user_features "
                    "WHERE user_id = :id AND enabled = false"
                ),
                {"id": uid},
            )).scalars())
        except Exception:
            # user_features missing (pre-migration) — no overrides.
            await s.rollback()
        return {
            "role": row["role"],
            "is_active": bool(row["is_active"]),
            "disabled_features": disabled,
        }


async def load_authz(user_id: str) -> dict:
    """Cached authz state for a user id (JWT sub, string).

    Failure semantics (deliberate, reviewed):
    - Redis down        -> fall through to the DB.
    - DB error          -> FAIL-OPEN to JWT-only claims with role='external'
                           (privileged surfaces CLOSE, data routes keep working;
                           a DB outage already breaks every data route anyway).
                           Loud error log.
    - row missing or
      is_active = false -> {"revoked": True}; caller raises 401. Cached too, so
                           a dead cookie doesn't hammer the DB.
    """
    key = _authz_key(user_id)
    r = None
    try:
        r = await _get_redis()
        cached = await r.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.warning("authz cache read failed (%s); falling back to DB", e)
        r = None

    try:
        authz = await _load_authz_db(user_id)
    except Exception as e:
        logger.error("authz DB lookup failed for user %s: %s — fail-open "
                     "as role='external' (pre-migration-035 or DB outage)",
                     user_id, e)
        return {"role": "external", "is_active": True, "disabled_features": []}

    if authz is None or not authz["is_active"]:
        authz = {"revoked": True}
    if r is not None:
        try:
            await r.setex(key, AUTHZ_CACHE_TTL_S, json.dumps(authz))
        except Exception:
            pass
    return authz


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
    user_id = payload.get("sub")
    authz = await load_authz(str(user_id))
    if authz.get("revoked"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="session_revoked"
        )
    return {
        "id": user_id,
        "email": payload.get("email"),
        "role": authz.get("role", "external"),
        "disabled_features": authz.get("disabled_features", []),
    }


async def require_internal_user(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") in ("admin", "internal"):
        return user
    # Env fallback: keeps internal surfaces working pre-migration-035 and
    # during the DB fail-open path (where role degrades to 'external').
    if (user.get("email") or "").lower() in INTERNAL_RESEARCH_EMAILS:
        return user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="internal_only")


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") == "admin":
        return user
    if (user.get("email") or "").lower() in ADMIN_EMAILS:
        return user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_only")


def require_feature(feature: str):
    """Dependency factory: 403 when the admin disabled `feature` for this user.

    No override row = enabled. Admin role bypasses every gate. Composes with
    the tier dependencies for free — get_current_user already carries
    disabled_features, so this adds zero extra lookups.
    """
    if feature not in FEATURE_REGISTRY:
        raise ValueError(f"unknown feature key: {feature}")

    async def _dep(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") != "admin" and feature in user.get("disabled_features", []):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="feature_disabled"
            )
        return user

    return _dep


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

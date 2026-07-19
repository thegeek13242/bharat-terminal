"""Auth endpoints — Google sign-in, session check, logout.

All routes here are public (no get_current_user guard). Access control happens
inside /auth/google via the app_users invite allowlist.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from bharat_terminal.api.auth import (
    COOKIE_SECURE,
    SESSION_COOKIE,
    SESSION_TTL_DAYS,
    create_session_token,
    get_active_user_by_email,
    get_current_user,
    record_login,
    verify_google_id_token,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class GoogleLoginBody(BaseModel):
    id_token: str


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


@router.post("/google")
async def login_with_google(body: GoogleLoginBody, response: Response):
    """Verify a Google ID token, enforce the invite allowlist, set a session cookie."""
    idinfo = await verify_google_id_token(body.id_token)
    email = idinfo.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="no_email")

    user = await get_active_user_by_email(email)
    if not user or not user["is_active"]:
        # Invite-only: not on the allowlist (or deactivated).
        raise HTTPException(status_code=403, detail="not_invited")

    await record_login(user["id"], idinfo)
    token = create_session_token(user["id"], user["email"])
    _set_session_cookie(response, token)
    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": idinfo.get("name") or user.get("name"),
            "picture": idinfo.get("picture") or user.get("picture"),
        }
    }


@router.get("/me")
async def me(current=Depends(get_current_user)):
    """Return the current session's user, or 401 if not signed in."""
    user = await get_active_user_by_email(current["email"]) if current.get("email") else None
    if not user or not user["is_active"]:
        # Session valid but the user was since removed/deactivated.
        raise HTTPException(status_code=401, detail="not_authenticated")
    from bharat_terminal.api.auth import INTERNAL_RESEARCH_EMAILS
    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user.get("name"),
            "picture": user.get("picture"),
            # Gates internal-only surfaces in the UI (research chat). The
            # backend enforces independently via require_internal_user.
            "internal": (user["email"] or "").lower() in INTERNAL_RESEARCH_EMAILS,
        }
    }


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    return {"ok": True}

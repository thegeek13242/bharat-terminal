"""DB-backed roles: load_authz cache semantics, revocation, admin/feature
gates, env bootstrap fallbacks. Pinned behaviors from the admin-panel design:

- deactivating a user kills the session on the next request (the old
  JWT-only flow honoured a deleted user's cookie for up to 7 days);
- DB outage FAILS OPEN to role='external' (privileged surfaces close,
  data routes keep working);
- admin bypasses feature gates; ADMIN_EMAILS env is a lockout fallback.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from bharat_terminal.api import auth


@pytest.fixture(autouse=True)
def _reset_auth(monkeypatch, fake_redis):
    """Fresh redis + deterministic secret for every test."""
    monkeypatch.setattr(auth, "_redis", None)

    async def _get_redis():
        return fake_redis
    monkeypatch.setattr(auth, "_get_redis", _get_redis)
    monkeypatch.setattr(auth, "APP_JWT_SECRET", "test-secret")
    yield


def _db_result(result):
    """Patchable _load_authz_db returning `result` (or raising it) and
    counting invocations."""
    calls = {"n": 0}

    async def _load(user_id):
        calls["n"] += 1
        if isinstance(result, Exception):
            raise result
        return result
    return _load, calls


class FakeRequest:
    def __init__(self, cookies):
        self.cookies = cookies


def _cookie_for(user_id="7", email="x@y.com"):
    return {auth.SESSION_COOKIE: auth.create_session_token(user_id, email)}


# ── load_authz ──────────────────────────────────────────────────────────────
async def test_cache_miss_then_hit(monkeypatch, fake_redis):
    loader, calls = _db_result(
        {"role": "internal", "is_active": True, "disabled_features": ["ltp"]})
    monkeypatch.setattr(auth, "_load_authz_db", loader)

    first = await auth.load_authz("7")
    second = await auth.load_authz("7")
    assert first["role"] == "internal" and first["disabled_features"] == ["ltp"]
    assert second == first
    assert calls["n"] == 1, "second call must be served from the cache"
    assert fake_redis.ttl["authz:user:7"] == auth.AUTHZ_CACHE_TTL_S


async def test_revoked_user_is_cached_and_401s(monkeypatch, fake_redis):
    loader, calls = _db_result(
        {"role": "external", "is_active": False, "disabled_features": []})
    monkeypatch.setattr(auth, "_load_authz_db", loader)

    assert (await auth.load_authz("7")).get("revoked") is True
    assert json.loads(fake_redis.kv["authz:user:7"])["revoked"] is True

    with pytest.raises(HTTPException) as e:
        await auth.get_current_user(FakeRequest(_cookie_for("7")))
    assert e.value.status_code == 401
    assert e.value.detail == "session_revoked"
    assert calls["n"] == 1  # 401 path also served from cache


async def test_missing_row_is_revoked(monkeypatch):
    loader, _ = _db_result(None)
    monkeypatch.setattr(auth, "_load_authz_db", loader)
    assert (await auth.load_authz("999")).get("revoked") is True


async def test_db_error_fails_open_to_external(monkeypatch):
    loader, _ = _db_result(RuntimeError("db down"))
    monkeypatch.setattr(auth, "_load_authz_db", loader)
    authz = await auth.load_authz("7")
    assert authz == {"role": "external", "is_active": True,
                     "disabled_features": []}


async def test_redis_down_falls_through_to_db(monkeypatch):
    async def _broken_redis():
        raise ConnectionError("redis down")
    monkeypatch.setattr(auth, "_get_redis", _broken_redis)
    loader, calls = _db_result(
        {"role": "admin", "is_active": True, "disabled_features": []})
    monkeypatch.setattr(auth, "_load_authz_db", loader)
    assert (await auth.load_authz("7"))["role"] == "admin"
    assert calls["n"] == 1


async def test_invalidate_deletes_cache_key(monkeypatch, fake_redis):
    loader, calls = _db_result(
        {"role": "external", "is_active": True, "disabled_features": []})
    monkeypatch.setattr(auth, "_load_authz_db", loader)
    await auth.load_authz("7")
    await auth.invalidate_authz(7)
    await auth.load_authz("7")
    assert calls["n"] == 2, "invalidation must force a DB re-read"


# ── get_current_user carries role + features ────────────────────────────────
async def test_current_user_shape(monkeypatch):
    loader, _ = _db_result(
        {"role": "internal", "is_active": True, "disabled_features": ["db_explorer"]})
    monkeypatch.setattr(auth, "_load_authz_db", loader)
    user = await auth.get_current_user(FakeRequest(_cookie_for("7", "a@b.in")))
    assert user == {"id": "7", "email": "a@b.in", "role": "internal",
                    "disabled_features": ["db_explorer"]}


# ── gates ───────────────────────────────────────────────────────────────────
async def test_require_admin_role_and_env_fallback(monkeypatch):
    assert (await auth.require_admin(
        {"role": "admin", "email": "x@y.com"}))["role"] == "admin"

    monkeypatch.setattr(auth, "ADMIN_EMAILS", {"boot@strap.in"})
    ok = await auth.require_admin({"role": "external", "email": "Boot@Strap.in"})
    assert ok["email"] == "Boot@Strap.in"

    with pytest.raises(HTTPException) as e:
        await auth.require_admin({"role": "internal", "email": "x@y.com"})
    assert e.value.status_code == 403 and e.value.detail == "admin_only"


async def test_require_internal_role_and_env_fallback(monkeypatch):
    for role in ("admin", "internal"):
        assert await auth.require_internal_user({"role": role, "email": "x@y.com"})
    monkeypatch.setattr(auth, "INTERNAL_RESEARCH_EMAILS", {"legacy@env.in"})
    assert await auth.require_internal_user(
        {"role": "external", "email": "legacy@env.in"})
    with pytest.raises(HTTPException):
        await auth.require_internal_user({"role": "external", "email": "n@o.in"})


async def test_require_feature_gate_and_admin_bypass():
    dep = auth.require_feature("watchlist")
    blocked = {"role": "external", "email": "x@y.com",
               "disabled_features": ["watchlist"]}
    with pytest.raises(HTTPException) as e:
        await dep(blocked)
    assert e.value.status_code == 403 and e.value.detail == "feature_disabled"

    assert await dep({**blocked, "role": "admin"})  # admin bypass
    assert await dep({"role": "external", "email": "x@y.com",
                      "disabled_features": []})


def test_require_feature_rejects_unknown_key():
    with pytest.raises(ValueError):
        auth.require_feature("not_a_feature")


def test_feature_registry_prefixes_match_routers():
    # Registry keys the admin UI exposes must map to real route prefixes;
    # this pins the strings so a router rename breaks loudly here.
    expected = {
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
    assert auth.FEATURE_REGISTRY == expected

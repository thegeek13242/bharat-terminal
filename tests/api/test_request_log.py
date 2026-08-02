"""Activity middleware: route templates not raw URLs, skip rules, sampling,
queue-drop-on-full. The hot-path contract is enqueue-only — nothing here may
block or raise into the request path."""
from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import Response

from bharat_terminal.api import auth, request_log
from bharat_terminal.api.request_log import (
    ActivityRecorder,
    RequestLogMiddleware,
    feature_for_path,
    record_event,
)


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setattr(auth, "APP_JWT_SECRET", "test-secret")


def _request(path: str, method="GET", user_id="7", route_path=None,
             authed=True) -> Request:
    headers = []
    if authed:
        token = auth.create_session_token(user_id, "x@y.com")
        headers.append((b"cookie", f"{auth.SESSION_COOKIE}={token}".encode()))
    scope = {
        "type": "http", "method": method, "path": path,
        "query_string": b"", "headers": headers,
    }
    if route_path is not None:
        class _Route:
            pass
        r = _Route()
        r.path = route_path
        scope["route"] = r
    return Request(scope)


@pytest.fixture
def mw(monkeypatch):
    middleware = RequestLogMiddleware(app=None)
    rec = ActivityRecorder()
    monkeypatch.setattr(request_log, "recorder", rec)
    return middleware, rec


def events(rec: ActivityRecorder) -> list[dict]:
    out = []
    while not rec._queue.empty():
        out.append(rec._queue.get_nowait())
    return out


# ── feature mapping ────────────────────────────────────────────────────────
def test_feature_for_path_longest_prefix():
    assert feature_for_path("/watchlist/") == "watchlist"
    assert feature_for_path("/internal/ltp/RELIANCE") == "ltp"
    assert feature_for_path("/earnings-surprises?days=30") == "surprises"
    assert feature_for_path("/deals/recent") == "investors"
    assert feature_for_path("/terminal-unknown") is None


# ── recording rules ────────────────────────────────────────────────────────
def test_records_route_template_not_raw_url(mw):
    middleware, rec = mw
    req = _request("/company/RELIANCE", route_path="/company/{symbol}")
    middleware._record(req, Response(status_code=200), 12.3)
    (e,) = events(rec)
    assert e["path"] == "/company/{symbol}", "must store the template"
    assert e["event_type"] == "api" and e["user_id"] == 7
    assert e["status"] == 200 and e["duration_ms"] == 12


def test_skips_unauthenticated_options_and_infra_paths(mw):
    middleware, rec = mw
    middleware._record(_request("/company/X", authed=False),
                       Response(status_code=200), 1)
    middleware._record(_request("/company/X", method="OPTIONS"),
                       Response(status_code=200), 1)
    for p in ("/health", "/auth/me", "/ws/feed", "/events/beacon"):
        middleware._record(_request(p), Response(status_code=200), 1)
    assert events(rec) == []


def test_sampled_paths_respect_sample_rate(mw, monkeypatch):
    middleware, rec = mw
    # Force the sampler to always skip…
    monkeypatch.setattr(request_log.random, "randrange", lambda n: 1)
    middleware._record(_request("/internal/ltp/X"), Response(status_code=200), 1)
    assert events(rec) == []
    # …then to always record.
    monkeypatch.setattr(request_log.random, "randrange", lambda n: 0)
    middleware._record(_request("/internal/ltp/X"), Response(status_code=200), 1)
    assert len(events(rec)) == 1
    # Non-sampled paths never consult the sampler.
    monkeypatch.setattr(request_log.random, "randrange",
                        lambda n: (_ for _ in ()).throw(AssertionError))
    middleware._record(_request("/watchlist/"), Response(status_code=200), 1)
    assert len(events(rec)) == 1


# ── queue bounds ───────────────────────────────────────────────────────────
def test_queue_full_drops_silently(monkeypatch):
    monkeypatch.setattr(request_log, "QUEUE_MAX", 2)
    rec = ActivityRecorder()
    for _ in range(5):
        rec.enqueue({"user_id": 1})
    assert rec._queue.qsize() == 2
    assert rec._dropped == 3


# ── beacon sink ────────────────────────────────────────────────────────────
def test_record_event_maps_feature_and_casts_id(monkeypatch):
    rec = ActivityRecorder()
    monkeypatch.setattr(request_log, "recorder", rec)
    record_event({"id": "7", "email": "x@y.com"}, "page_view", "/watchlist")
    (e,) = events(rec)
    assert e == {
        "user_id": 7, "email": "x@y.com", "event_type": "page_view",
        "method": None, "path": "/watchlist", "status": None,
        "duration_ms": None, "feature": "watchlist",
    }
    record_event({"id": "not-a-number"}, "page_view", "/x")  # must not raise
    assert events(rec) == []

"""Redis→Postgres flushers: batch drain, malformed-entry tolerance,
DB-failure push-back. These carry the durable alert history and LLM cost
log — silent loss here means the admin panel lies."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from bharat_terminal.api import flushers


def _llm_entry_dict(call_id="c1", **over):
    return json.loads(_llm_entry(call_id, **over))


def _llm_entry(call_id="c1", **over):
    base = {
        "call_id": call_id, "timestamp": "2026-08-01T10:00:00+00:00",
        "service": "abc123", "model": "gemini-3.1-pro-preview",
        "stage": "dossier", "context_id": "RELIANCE",
        "prompt_tokens": 100, "completion_tokens": 50, "thinking_tokens": 10,
        "cached_tokens": 0, "web_search_calls": 1, "turns": 5,
        "latency_ms": 900.5, "cost_usd": 0.01, "success": True, "error": None,
    }
    base.update(over)
    return json.dumps(base)


async def test_drain_inserts_batch_and_empties_queue(monkeypatch, fakes):
    r = fakes.FakeRedis()
    r.lists["llm:calls"] = [_llm_entry("c1"), _llm_entry("c2")]
    s = fakes.FakeSession([("INSERT INTO llm_call_log", fakes.Rows())])
    monkeypatch.setattr(flushers, "get_session_factory",
                        lambda: fakes.fake_factory(s))

    n = await flushers._drain_queue(r, "llm:calls", flushers._LLM_INSERT,
                                    flushers._llm_params)
    assert n == 2
    assert r.lists["llm:calls"] == []
    assert s.committed
    (_, params), = [(q, p) for q, p in s.executed]
    assert isinstance(params, list) and len(params) == 2  # executemany
    assert {p["call_id"] for p in params} == {"c1", "c2"}
    assert params[0]["context_id"] == "RELIANCE"


async def test_malformed_entry_skipped_batch_survives(monkeypatch, fakes):
    r = fakes.FakeRedis()
    r.lists["llm:calls"] = [_llm_entry("ok"), "not json {{{"]
    s = fakes.FakeSession([("INSERT INTO llm_call_log", fakes.Rows())])
    monkeypatch.setattr(flushers, "get_session_factory",
                        lambda: fakes.fake_factory(s))

    n = await flushers._drain_queue(r, "llm:calls", flushers._LLM_INSERT,
                                    flushers._llm_params)
    assert n == 1, "good entry inserted, bad one skipped"


async def test_db_failure_pushes_batch_back(monkeypatch, fakes):
    r = fakes.FakeRedis()
    r.lists["alerts:history"] = [json.dumps(
        {"ts": "2026-08-01T10:00:00+00:00", "service": "svc",
         "kind": "PIPELINE_STALL", "subject": "s", "body": "b",
         "delivered": True})]
    s = fakes.FakeSession([("INSERT INTO alert_events", RuntimeError("db down"))])
    monkeypatch.setattr(flushers, "get_session_factory",
                        lambda: fakes.fake_factory(s))

    n = await flushers._drain_queue(r, "alerts:history", flushers._ALERT_INSERT,
                                    flushers._alert_params)
    assert n == 0
    assert len(r.lists["alerts:history"]) == 1, "entry must survive for retry"
    assert not s.committed


def test_ts_params_are_datetimes_not_strings():
    """Regression (prod 2026-08-03): ts was passed through as an ISO STRING.
    asyncpg binds timestamptz positionally and DataErrors the whole
    executemany batch on a str, so every LLM cost record silently backed up
    in Redis (2,824 queued, 0 inserted) while the fake-session tests stayed
    green. Pin the type, not just the value."""
    llm = flushers._llm_params(_llm_entry_dict())
    assert isinstance(llm["ts"], datetime)
    assert llm["ts"].tzinfo is not None

    alert = flushers._alert_params({"ts": "2026-08-01T10:00:00+00:00"})
    assert isinstance(alert["ts"], datetime)

    # Naive input is assumed UTC; junk falls back to now() rather than
    # poisoning the batch.
    assert flushers._ts("2026-08-01T10:00:00").tzinfo is timezone.utc
    assert isinstance(flushers._ts("not-a-timestamp"), datetime)
    assert isinstance(flushers._ts(None), datetime)


def test_param_mappers_tolerate_missing_fields():
    llm = flushers._llm_params({"call_id": "x"})
    assert llm["model"] == "unknown" and llm["prompt_tokens"] == 0
    assert llm["context_id"] is None and llm["success"] is True

    alert = flushers._alert_params({})
    assert alert["service"] == "unknown" and alert["kind"] == "UNKNOWN"
    assert alert["delivered"] is None

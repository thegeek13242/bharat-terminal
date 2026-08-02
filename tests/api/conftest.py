"""Shared fakes for the api-layer admin-panel suite.

Same doctrine as tests/papertrade/conftest.py: no test here may touch the
network, Redis, or a database — scripts/test.sh points every infra env var
at an unresolvable host so an escape fails loudly. FakeRedis/FakeSession
cover exactly what auth.py / flushers.py / routes/admin.py call; unhandled
SQL raises AssertionError so new production queries can't silently no-op.
"""
from __future__ import annotations

import pytest


class FakeRedis:
    """Async-redis stand-in: string KV with TTL bookkeeping + list queues."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.lists: dict[str, list[str]] = {}
        self.calls: list[tuple] = []

    async def get(self, key):
        self.calls.append(("get", key))
        return self.kv.get(key)

    async def setex(self, key, ttl, value):
        self.calls.append(("setex", key, ttl))
        self.kv[key] = value
        self.ttl[key] = ttl

    async def delete(self, key):
        self.calls.append(("delete", key))
        self.kv.pop(key, None)
        self.ttl.pop(key, None)

    # list ops (flusher queues) — RPOP with count, RPUSH for push-back
    async def rpop(self, key, count=None):
        q = self.lists.setdefault(key, [])
        if not q:
            return None
        n = 1 if count is None else min(count, len(q))
        out = [q.pop() for _ in range(n)]
        return out[0] if count is None else out

    async def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)


class _Rows:
    """Wraps a scripted result so .mappings().all()/.first()/.scalars()/
    .scalar() and .rowcount behave like SQLAlchemy's Result."""

    def __init__(self, rows=None, scalar=None, rowcount=0):
        self.rows = rows or []
        self._scalar = scalar
        self.rowcount = rowcount

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalars(self):
        return [r[next(iter(r))] if isinstance(r, dict) else r for r in self.rows]

    def scalar(self):
        return self._scalar

    def __iter__(self):
        return iter(self.rows)


class FakeSession:
    """Dispatches executed SQL against `responders`: a list of
    (substring, result_or_callable). First match wins; no match asserts."""

    def __init__(self, responders):
        self.responders = responders
        self.executed: list[tuple[str, object]] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.executed.append((sql, params))
        for frag, result in self.responders:
            if frag.lower() in sql.lower():
                if callable(result):
                    result = result(sql, params)
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"FakeSession: unhandled SQL: {sql[:160]}")

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def fake_factory(session: FakeSession):
    """A get_session_factory() stand-in returning the given session."""
    def factory():
        return session
    return factory


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def fakes():
    """Class access for tests (tests/ is not a package — papertrade
    precedent — so fixtures are the import path)."""
    from types import SimpleNamespace
    return SimpleNamespace(
        FakeRedis=FakeRedis,
        FakeSession=FakeSession,
        Rows=_Rows,
        fake_factory=fake_factory,
    )

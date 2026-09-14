"""Two role-scoped connection pools, and the guard that makes them meaningful.

Migration 044 created `factory_api` (member of factory_writer) and
`factory_api_ro` (member of factory_reader). Verified against live sessions on
2026-09-14:

    as factory_api     INSERT factory.gate_decisions -> permission denied
                       SELECT public.price_points    -> permission denied
    as factory_api_ro  SELECT public.paper_trades    -> allowed
                       INSERT factory.hypotheses     -> permission denied

Fifteen of the sixteen read tools use the WRITER pool, which holds no grants on
`public` at all; only `get_paper_feedback` uses the READER pool. That split is
the reason the tool with production reach is the visibly privileged one.

THE SUPERUSER GUARD IS THE POINT
--------------------------------
Before migration 044 the only login role was `bharat`, a superuser, and
superusers bypass every GRANT and REVOKE -- so the careful grants in 043
protected nothing. A misconfigured DSN would quietly restore exactly that
state, and nothing downstream would notice: every query would simply succeed.
`assert_not_superuser` turns that silent regression into a refusal to start.

Nothing here is imported by the read tools, and tests never construct a pool.
`scripts/test.sh` points DATABASE_URL at an unresolvable host on purpose.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List

# asyncpg is present in the runtime image but not needed to import this module;
# keeping the import local means `reads.py` tests never drag in a driver.
_WRITER_ENV = "FACTORY_DB_URL"
_READER_ENV = "FACTORY_DB_RO_URL"


class SuperuserRefused(RuntimeError):
    """Raised when a pool would connect as a superuser. Fail closed."""


@dataclass
class PoolSpec:
    dsn_env: str
    expected_user: str
    min_size: int = 1
    max_size: int = 5

    def dsn(self) -> str:
        dsn = os.getenv(self.dsn_env)
        if not dsn:
            raise RuntimeError(
                f"{self.dsn_env} is not set. The tools API refuses to guess a "
                f"connection string; set it from Secrets Manager.")
        return dsn


WRITER = PoolSpec(_WRITER_ENV, "factory_api", max_size=5)
READER = PoolSpec(_READER_ENV, "factory_api_ro", max_size=3)


async def assert_not_superuser(conn: Any) -> str:
    """Refuse to serve on a superuser connection, and say who we actually are.

    Returns the current user so the caller can log it. Raises SuperuserRefused
    otherwise -- never a warning, because a warning in a log nobody reads is
    how the 043 boundary went unnoticed in the first place.
    """
    row = await conn.fetchrow(
        "SELECT current_user AS usr, "
        "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_super")
    user = row["usr"]
    if row["is_super"]:
        raise SuperuserRefused(
            f"tools API connected as '{user}', which is a SUPERUSER. Superusers "
            f"bypass every GRANT and REVOKE, so the ledger privilege boundary "
            f"would not exist. Refusing to start.")
    return user


async def open_pool(spec: PoolSpec) -> Any:
    """Open one role-scoped pool and verify who it connected as."""
    import asyncpg  # local: keeps the driver out of unit-test imports

    pool = await asyncpg.create_pool(
        dsn=spec.dsn(), min_size=spec.min_size, max_size=spec.max_size,
        command_timeout=30)
    async with pool.acquire() as conn:
        user = await assert_not_superuser(conn)
        if user != spec.expected_user:
            await pool.close()
            raise RuntimeError(
                f"{spec.dsn_env} connected as '{user}', expected "
                f"'{spec.expected_user}'. Refusing to serve on the wrong role.")
    return pool


def make_executor(pool: Any):
    """Adapt an asyncpg pool to the `Executor` the read tools expect.

    Named parameters (:name) are translated to asyncpg's positional $n by
    `to_positional`. Values are always bound, never interpolated -- so
    `search_memory`'s embedding reaches the database as a bound parameter,
    never as text inside a statement.
    """

    async def execute(sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        positional, order = to_positional(sql)
        args = [params.get(n) for n in order]
        async with pool.acquire() as conn:
            rows = await conn.fetch(positional, *args)
        return [dict(r) for r in rows]

    return execute


def to_positional(sql: str):
    """Translate :name placeholders to asyncpg's $n. Returns (sql, names).

    The `(?<!:)` lookbehind is load-bearing and was found the hard way: a naive
    `:(\\w+)` also matches the second colon of a PostgreSQL cast, so `::text`,
    `::date`, `::vector` and `::interval` were being rewritten into `$n` and
    six of seventeen tools failed with "syntax error at or near \\":\\"". Unit
    tests with a fake executor never see this, because they never parse SQL.
    """
    import re

    order: List[str] = []

    def sub(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name not in order:
            order.append(name)
        return f"${order.index(name) + 1}"

    return re.sub(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", sub, sql), order

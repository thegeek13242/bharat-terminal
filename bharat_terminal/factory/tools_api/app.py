"""The research-tools HTTP service.

One POST per tool. The Lambda proxy calls this over the VPC private IP; no
route is ever exposed publicly, and the app binds to the private interface.

    agent -> AgentCore Gateway (+Cedar) -> Lambda (VPC) -> THIS -> Postgres

It is also directly usable without any AWS at all, which is how the alpha runs:
the same dispatch table serves an in-process agent and the Lambda.

THREE THINGS THIS SERVICE WILL NOT DO
  1. Start as a superuser. `open_pool` calls `assert_not_superuser`, so a
     misconfigured DSN is a refusal to boot rather than a silent restoration of
     full database reach. That failure mode is exactly how migration 043's
     boundary went unnoticed for two days.
  2. Serve a tool it does not know. The dispatch table is a closed dict; an
     unrecognised name is a 404, never a passthrough.
  3. Reach production data from the writer pool. Fifteen tools run on
     `factory_api`, which holds no grant on `public`. Only get_paper_feedback
     uses the reader pool, so the one tool with production reach is the
     visibly privileged one.

AUTH is a bearer token from FACTORY_TOOLS_TOKEN, compared with
`secrets.compare_digest`. The agents never see it: the Lambda holds it and
fetches it from Secrets Manager.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import date
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException

from bharat_terminal.factory.tools_api import reads, writes
from bharat_terminal.factory.tools_api.pools import PoolSpec, make_executor, open_pool

logger = logging.getLogger(__name__)

WRITER = PoolSpec("FACTORY_DB_URL", "factory_api", max_size=5)
READER = PoolSpec("FACTORY_DB_RO_URL", "factory_api_ro", max_size=3)

_state: Dict[str, Any] = {"writer": None, "reader": None}


# ── auth ─────────────────────────────────────────────────────────────────

def require_token(authorization: Optional[str] = Header(None)) -> None:
    """Constant-time bearer check. Absent token configured = refuse everything."""
    expected = os.getenv("FACTORY_TOOLS_TOKEN")
    if not expected:
        raise HTTPException(503, "FACTORY_TOOLS_TOKEN is not configured")
    supplied = (authorization or "")[7:] if (authorization or "").startswith("Bearer ") else ""
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "bad or missing bearer token")


# ── dispatch ─────────────────────────────────────────────────────────────

def _writer():
    ex = _state.get("writer")
    if ex is None:
        raise HTTPException(503, "writer pool is not open")
    return ex


def _reader():
    ex = _state.get("reader")
    if ex is None:
        raise HTTPException(503, "reader pool is not open")
    return ex


async def _read_only(name: str, fn: Callable[..., Awaitable[Any]], a: Dict[str, Any]):
    return await fn(_writer(), **a)


# Every entry is (handler, uses_reader_pool). A closed table: a tool that is
# not here cannot be reached, which is what makes "there is no promote tool" a
# property of the service rather than of its documentation.
DISPATCH: Dict[str, Callable[[Dict[str, Any]], Awaitable[Any]]] = {}


def _register() -> None:
    async def search_memory(a):
        from bharat_terminal.llm.embeddings import embed_query

        async def embedder(q: str):
            return (await embed_query(q)) or [0.0] * 1024

        return await reads.search_memory(
            _writer(), a.get("query", ""), embedder=embedder,
            k=int(a.get("k", 8)), family_id=a.get("family_id"),
            include_quarantined=bool(a.get("include_quarantined", False)))

    DISPATCH.update({
        # ── reads on the writer pool (no production reach) ──
        "search_memory": search_memory,
        "get_failure": lambda a: reads.get_failure(_writer(), a["failure_id"]),
        "get_hypothesis": lambda a: reads.get_hypothesis(
            _writer(), a["hypothesis_id"], a.get("version"),
            include_spec=bool(a.get("include_spec", False))),
        "get_research_question": lambda a: reads.get_research_question(
            _writer(), a["question_id"]),
        "get_family_state": lambda a: reads.get_family_state(_writer(), a.get("family_id")),
        "get_dataset_catalog": lambda a: reads.get_dataset_catalog(
            _writer(), int(a.get("limit", 5))),
        "get_run_results": lambda a: reads.get_run_results(_writer(), a["experiment_id"]),
        "get_gate_decision": lambda a: reads.get_gate_decision(
            _writer(), a["hypothesis_id"], a.get("version")),
        "get_holdout_status": lambda a: reads.get_holdout_status(_writer(), a["hypothesis_id"]),
        "get_divergence_report": lambda a: reads.get_divergence_report(_writer(), a["report_id"]),
        "get_frontier": lambda a: reads.get_frontier(_writer()),
        "get_budget": lambda a: reads.get_budget(_writer(), int(a.get("days", 30))),
        "get_report_facts": lambda a: reads.get_report_facts(
            _writer(), date.fromisoformat(a["report_date"])),

        # ── the ONE tool with production reach, on the reader pool ──
        "get_paper_feedback": lambda a: reads.get_paper_feedback(
            _reader(), a.get("strategy_id"),
            date.fromisoformat(a["since"]) if a.get("since") else None),
    })


_register()

# Code-backed tools: no database, no pool. A feature and an attack are code
# artifacts that arrive by pull request, never by agent write.
SYNC_DISPATCH: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "list_features": lambda a: reads.list_features(),
    "get_battery_catalog": lambda a: reads.get_battery_catalog(),
}


def register_writes(compile_ctx_factory: Callable[[], Awaitable[Any]]) -> None:
    """Write tools need a CompileContext built from the LEDGER, never from the
    agent. The factory is injected so the service can rebuild it per request as
    the snapshot rolls over."""

    async def submit_hypotheses(a):
        ctx = await compile_ctx_factory()
        return await writes.submit_hypotheses(
            _writer(), a.get("hypotheses") or [], ctx=ctx,
            agent_version_id=a["agent_version_id"],
            agent_run_id=a.get("agent_run_id"), trace_id=a.get("trace_id"),
            attempt=int(a.get("attempt", 1)))

    DISPATCH.update({
        "submit_hypotheses": submit_hypotheses,
        "submit_attack_plan": lambda a: writes.submit_attack_plan(
            _writer(), a["experiment_id"], a.get("attacks") or [], a.get("kill_memo", ""),
            agent_version_id=a["agent_version_id"], agent_run_id=a.get("agent_run_id"),
            trace_id=a.get("trace_id")),
        "submit_allocation": lambda a: writes.submit_allocation(
            _writer(), agent_version_id=a["agent_version_id"],
            allocations=a.get("allocations") or (), kills=a.get("kills") or (),
            revisits=a.get("revisits") or (), global_cap_usd=a.get("global_cap_usd"),
            agent_run_id=a.get("agent_run_id"), trace_id=a.get("trace_id")),
        "submit_report_narrative": lambda a: writes.submit_report_narrative(
            _writer(), date.fromisoformat(a["report_date"]), a.get("sections") or {},
            agent_version_id=a["agent_version_id"], trace_id=a.get("trace_id")),
        "report_data_gap": lambda a: writes.report_data_gap(
            _writer(), a.get("need", ""), agent_version_id=a["agent_version_id"],
            family_id=a.get("family_id"), blocked_tool=a.get("blocked_tool"),
            suggested_source=a.get("suggested_source"),
            evidence_refs=a.get("evidence_refs") or (),
            agent_run_id=a.get("agent_run_id"), trace_id=a.get("trace_id")),
    })


# ── routes ───────────────────────────────────────────────────────────────

router = APIRouter(prefix="/tools", tags=["factory-tools"])


@router.get("/_health")
async def health() -> Dict[str, Any]:
    """Names who the pools connected as. Useful precisely because a wrong
    answer here (`bharat`) means the boundary is gone."""
    return {
        "ok": _state.get("writer") is not None,
        "writer_user": _state.get("writer_user"),
        "reader_user": _state.get("reader_user"),
        "tools": sorted(set(DISPATCH) | set(SYNC_DISPATCH)),
    }


@router.post("/{tool_name}", dependencies=[Depends(require_token)])
async def call_tool(tool_name: str, args: Dict[str, Any] = Body(default={})) -> Any:
    if tool_name in SYNC_DISPATCH:
        return SYNC_DISPATCH[tool_name](args)
    handler = DISPATCH.get(tool_name)
    if handler is None:
        # Closed table. An unknown name is refused, never guessed at.
        raise HTTPException(404, f"no such tool: {tool_name}")
    try:
        return await handler(args)
    except KeyError as e:
        raise HTTPException(422, f"missing required argument: {e}") from e
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - the boundary reports, never leaks a trace
        logger.exception("tool %s failed", tool_name)
        raise HTTPException(500, f"{type(e).__name__}") from e


def create_app() -> FastAPI:
    app = FastAPI(title="factory research-tools", docs_url=None, redoc_url=None)

    @app.on_event("startup")
    async def _startup() -> None:
        wp = await open_pool(WRITER)          # refuses a superuser connection
        _state["writer"] = make_executor(wp)
        async with wp.acquire() as c:
            _state["writer_user"] = await c.fetchval("SELECT current_user")
        if os.getenv(READER.dsn_env):
            rp = await open_pool(READER)
            _state["reader"] = make_executor(rp)
            async with rp.acquire() as c:
                _state["reader_user"] = await c.fetchval("SELECT current_user")
        else:
            logger.warning("%s unset - get_paper_feedback will 503", READER.dsn_env)

    app.include_router(router)
    return app

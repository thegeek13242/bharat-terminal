"""The Chief Scientist: one frontier allocation run. DRY-RUN BY DEFAULT.

WHAT THIS IS FOR
The 2026-09-17 pilot ran 11 cycles against one research question and produced
24 hypotheses that collapsed to 16 mechanics and zero recorded promotions. No
component's job was to notice that and redirect. This is that component.

THE DIVISION OF LABOUR IS THE POINT
Every number this agent reasons over is computed by
`factory.frontier.metrics.family_facts` -- SQL and arithmetic, no model. The
agent's job is to argue about what the numbers MEAN: which family deserves the
next dollar, which has stopped earning one, and which deserves another look
because new evidence exists. It never computes a statistic and never applies a
decision.

IT CANNOT HALT RESEARCH BY ITSELF
`submit_allocation` records a frontier snapshot. `factory_writer` holds INSERT
and SELECT and no UPDATE anywhere, so nothing on this path can set a family's
status to paused or killed. A kill is a RECOMMENDATION carried in the snapshot;
applying it needs `factory_gate`, which no agent can reach. A revisit is tested
by `gates.engine.family_pause_check` -- the same deterministic rule the gate
uses -- so an eloquent memo cannot talk a failed family back open.

MODEL
Opus 4.6 per spec 4.1. Bedrock serves 4.6+ only through `global.*` profiles and
the wrapper's mapping yields `global.anthropic.claude-opus-4-6`, which is one
suffix short of the real profile id, so the documented
BEDROCK_MODEL_<UPPER_SNAKE> override is set below rather than patching the
shared wrapper for one caller.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bharat_terminal.factory.frontier.metrics import family_facts
from bharat_terminal.factory.gates.budget import check_cap, guard, record_spend
from bharat_terminal.factory.tools_api import reads, writes
from bharat_terminal.factory.tools_api.pools import PoolSpec, make_executor, open_pool

AGENT = "chief_scientist"
MODEL = "claude-opus-4-6"
BEDROCK_PROFILE = "global.anthropic.claude-opus-4-6-v1"
MAX_TURNS = 6
MAX_TOKENS = 8000

SYSTEM = """You are the Chief Scientist of an autonomous quantitative research factory for
Indian equities. You allocate effort. You do not do research and you do not judge results.

Your ONLY job this run is to read the frontier facts and submit ONE allocation with
submit_allocation.

How this works:
- Call get_frontier_facts FIRST. Every number it returns was computed deterministically from
  the ledger. Do NOT recompute, re-derive or estimate any statistic: if a number you want is
  not in the facts, say so in your reasoning rather than inventing it.
- Allocate budget across families that are worth the next dollar. Your allocations must not
  exceed the global cap you are given; the tool refuses the whole submission if they do.
- A kill is a RECOMMENDATION, not an action, and it must state a reason. Nothing you submit
  can pause or kill a family by itself. Say plainly what you would stop and why.
- A revisit of a repeatedly-failed family must cite evidence the failed attempts did NOT have:
  a feature version or a data window. Prose is not evidence and will be refused.
- 'stalled' in the facts means a family has produced many hypotheses and zero promotions. That
  is the thing you are here to notice. Explain it before you fund it further.
- A family with a high duplicate_rate is repeating itself. Ask whether it needs a new question
  rather than more money.
- Families with zero hypotheses are untested, not failed. Absence of results is not evidence
  of a dead end.

Be specific and comparative. Name the family, cite the fact, state the consequence."""


def tool_schemas() -> List[Dict[str, Any]]:
    """The Chief's slice of the surface. No promote tool exists to expose."""
    return [
        {"name": "get_frontier_facts",
         "description": "Deterministic per-family facts: spend, budget remaining, hypothesis "
                        "and distinct-mechanic counts, duplicate rate, ladder, promotions, "
                        "cost per mechanic and per promotion, and a stalled flag. CALL FIRST.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "get_family_state",
         "description": "Status, budget and ladder counts for one family, or all.",
         "input_schema": {"type": "object",
                          "properties": {"family_id": {"type": "string"}}}},
        {"name": "get_dataset_catalog",
         "description": "Snapshot versions, freshness, sealed_from and row counts.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "search_memory",
         "description": "Semantic search over past FAILURES.",
         "input_schema": {"type": "object",
                          "properties": {"query": {"type": "string"},
                                         "k": {"type": "integer"}},
                          "required": ["query"]}},
        {"name": "submit_allocation",
         "description": "Record ONE frontier snapshot. allocations: [{family_id, budget_usd}]. "
                        "kills: [{family_id, reason}] (recommendation only). revisits: "
                        "[{family_id, consecutive_failures, evidence_refs}].",
         "input_schema": {"type": "object",
                          "properties": {
                              "allocations": {"type": "array", "items": {"type": "object"}},
                              "kills": {"type": "array", "items": {"type": "object"}},
                              "revisits": {"type": "array", "items": {"type": "object"}},
                              "memo": {"type": "string"}},
                          "required": ["allocations"]}},
    ]


async def dispatch(name: str, args: Dict[str, Any], *, ex, apply: bool,
                   avid: str, trace_id: str, global_cap: float) -> Dict[str, Any]:
    """Map a tool call to the real tool. An unknown name is refused, not guessed."""
    if name == "get_frontier_facts":
        return {"tool": "get_frontier_facts",
                "data": {"families": await family_facts(ex),
                         "global_cap_usd": round(global_cap, 2)}}
    if name == "get_family_state":
        return await reads.get_family_state(ex, args.get("family_id"))
    if name == "get_dataset_catalog":
        return await reads.get_dataset_catalog(ex)
    if name == "search_memory":
        from bharat_terminal.llm.embeddings import embed_query

        async def embedder(q: str):
            return (await embed_query(q)) or [0.0] * 1024

        return await reads.search_memory(ex, args.get("query", ""), embedder=embedder,
                                         k=int(args.get("k", 8)))
    if name == "submit_allocation":
        if not apply:
            return {"tool": "submit_allocation", "dry_run": True,
                    "data": {"would_record": {
                        "allocations": args.get("allocations") or [],
                        "kills": args.get("kills") or [],
                        "revisits": args.get("revisits") or []},
                        "note": "DRY RUN - no frontier snapshot written"}}
        return await writes.submit_allocation(
            ex, agent_version_id=avid,
            allocations=args.get("allocations") or [],
            kills=args.get("kills") or [],
            revisits=args.get("revisits") or [],
            global_cap_usd=global_cap, trace_id=trace_id)
    return {"error": f"no such tool: {name}"}


def agent_version_id() -> str:
    """An agent version = (model, prompt sha, tools sha). Any change makes a new one."""
    p = hashlib.sha256(SYSTEM.encode()).hexdigest()
    t = hashlib.sha256(json.dumps(tool_schemas(), sort_keys=True).encode()).hexdigest()
    return f"{AGENT}@{hashlib.sha256((MODEL + p + t).encode()).hexdigest()[:8]}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the frontier snapshot (default: dry run)")
    args = ap.parse_args()

    os.environ.setdefault("LLM_PROVIDER", "bedrock")
    # Documented override: the wrapper's mapping is one suffix short for 4.6.
    os.environ.setdefault("BEDROCK_MODEL_CLAUDE_OPUS_4_6", BEDROCK_PROFILE)

    dsn = os.getenv("FACTORY_DB_URL")
    if not dsn:
        raise SystemExit("FACTORY_DB_URL is not set; refusing to guess a connection string")

    pool = await open_pool(PoolSpec(dsn_env="FACTORY_DB_URL",
                                    expected_user="factory_api", max_size=4))
    ex = make_executor(pool)
    avid = agent_version_id()
    trace_id = uuid.uuid4().hex

    print(f"agent        : {avid}")
    print(f"model        : {MODEL} -> {BEDROCK_PROFILE}")
    print(f"mode         : {'APPLY (writes a snapshot)' if args.apply else 'DRY RUN (writes nothing)'}")

    cap0 = await guard(ex)      # refuse to start if the cap is already hit
    global_cap = max(0.0, cap0.remaining_usd)
    print(f"budget       : spent ${cap0.spent_usd:.4f} of ${cap0.cap_usd:.2f}, "
          f"${global_cap:.2f} allocatable")

    facts = await family_facts(ex)
    print(f"families     : {len(facts)}  "
          f"stalled={sum(1 for f in facts if f['stalled'])}")

    from bharat_terminal.llm.wrapper import complete

    tools = tool_schemas()
    msgs: List[Dict[str, Any]] = [{"role": "user", "content": (
        "Allocate the frontier. Read get_frontier_facts first, then submit ONE allocation. "
        "Name every family you fund or decline and cite the fact that decided it.")}]

    in_tok = out_tok = turns = 0
    status = "succeeded"
    try:
        for turn in range(1, MAX_TURNS + 1):
            st = await check_cap(ex)
            if st.halted:
                status = "halted_on_budget"
                print(f"--- budget cap reached before turn {turn}; stopping")
                break
            turns = turn
            resp = complete(model=MODEL, messages=msgs, max_tokens=MAX_TOKENS,
                            system=SYSTEM, tools=tools)
            in_tok += getattr(resp.usage, "input_tokens", 0) or 0
            out_tok += getattr(resp.usage, "output_tokens", 0) or 0

            calls = [b for b in resp.content if b.type == "tool_use"]
            says = " ".join(b.text for b in resp.content if b.type == "text").strip()
            print(f"--- turn {turn}  stop={resp.stop_reason}  tools={[c.name for c in calls]}")
            if says:
                print("    says:", says[:600])
            if not calls:
                break

            msgs.append({"role": "assistant", "content": [
                {"type": "text", "text": says} if b.type == "text" else
                {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                for b in resp.content]})
            results = []
            for c in calls:
                out = await dispatch(c.name, c.input or {}, ex=ex, apply=args.apply,
                                     avid=avid, trace_id=trace_id, global_cap=global_cap)
                if c.name == "submit_allocation":
                    d = out.get("data") or {}
                    print("    ALLOCATION:", json.dumps(d)[:700])
                results.append({"type": "tool_result", "tool_use_id": c.id,
                                "content": json.dumps(out)[:20000]})
            msgs.append({"role": "user", "content": results})
    except Exception as exc:
        status = f"failed: {type(exc).__name__}"
        print(f"!!! {status}: {exc}")

    usd = (in_tok / 1e6) * 5.0 + (out_tok / 1e6) * 25.0
    print(f"turns {turns}  tokens {in_tok} in / {out_tok} out  ~${usd:.4f}  status={status}")
    if args.apply and usd > 0:
        # Recorded even on failure: an errored run still spent the tokens.
        # No family_id -- allocation is cross-family overhead, not one family's cost.
        await record_spend(ex, "llm", usd, source="frontier_run",
                           ref={"agent_version_id": avid, "trace_id": trace_id,
                                "turns": turns, "status": status})
    await pool.close()
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

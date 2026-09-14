"""Write tools. Currently one: `submit_hypotheses`.

Every write tool obeys the same four rules:

  1. INSERT-only, into `factory.*` only. Never UPDATE, never `gate_decisions`,
     never `hypothesis_status`, never a production table. Postgres enforces
     this independently: `factory_api` is a member of `factory_writer`, which
     was REVOKEd on exactly those tables (migration 044, verified against a
     real login session).
  2. The compiler is the ONLY validator. A second opinion implemented here
     would drift from `compile_spec` and start accepting what the cycle later
     rejects, so this module never re-decides what the compiler decided.
  3. Errors are returned VERBATIM, because they are repair instructions. The
     Scientist gets `{code, path, message}` per failure and may retry twice.
  4. Nothing here opens a connection. The executor is injected, exactly as in
     `reads.py` and `workflows.ledger.LedgerWriter`.

TWO HONEST LIMITATIONS, both schema gaps rather than choices
------------------------------------------------------------
**Idempotency is weaker than the design specifies.** The design calls for a key
of (agent_run_id, call_seq); no such column exists anywhere in `factory.*`
(verified 2026-09-14). What does exist is the `hypotheses` primary key
(hypothesis_id, version) and `uq_lineage_edge`, so `ON CONFLICT DO NOTHING`
makes a replayed identical submission a no-op. That covers a Gateway retry of
the same payload. It does NOT cover an agent that regenerates ids on retry --
that needs an idempotency table and a migration.

**Cross-submit deduplication is incomplete.** `ctx.known_spec_hashes` maps
spec_hash -> experiment_id, and an experiment only exists once a cycle runs. A
hypothesis row carries no spec_hash column, so two agents can submit identical
mechanics and both land as hypotheses; they collide later at
`experiments.spec_hash` (UNIQUE) instead of at submit time. Within a single
batch this module DOES dedupe, because that case is free.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from bharat_terminal.factory.dsl.compiler import (
    CompileContext,
    CompileResult,
    compile_spec,
)
from bharat_terminal.factory.dsl.models import HypothesisSpec
from bharat_terminal.factory.tools_api.envelope import envelope
from bharat_terminal.factory.tools_api.reads import Executor

# The Scientist proposes in small batches so a bad frontier slot cannot flood
# the ledger, and so compiler errors stay legible enough to repair.
MAX_PER_CALL = 5

# After this many repair attempts the slot is abandoned and a `failures` row is
# written. Two is deliberate: the first retry fixes a typo, the second fixes a
# misunderstanding, and a third usually means the agent cannot see why.
MAX_REPAIR_ATTEMPTS = 2


async def submit_hypotheses(
    ex: Executor,
    hypotheses: Sequence[Dict[str, Any]],
    *,
    ctx: CompileContext,
    agent_version_id: str,
    agent_run_id: Optional[int] = None,
    trace_id: Optional[str] = None,
    attempt: int = 1,
) -> Dict[str, Any]:
    """Validate and record proposed hypotheses.

    Each item is `{spec: {...}, parents: [{id, version, op}], mechanism: str}`.
    `spec` must be a complete HypothesisSpec; the id inside it is the agent's
    own, which is what makes a retry idempotent.

    Returns per-item results. A rejected item carries its compiler errors so
    the agent can repair and resubmit -- rejection is a conversation, not a
    dead end.
    """
    if not hypotheses:
        return envelope("submit_hypotheses",
                        {"accepted": [], "rejected": [], "attempt": attempt},
                        note="no hypotheses submitted")

    over_cap = len(hypotheses) > MAX_PER_CALL
    batch = list(hypotheses[:MAX_PER_CALL])

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    seen_hashes: Dict[str, str] = {}

    for idx, item in enumerate(batch):
        spec, parse_err = _parse_spec(item)
        if spec is None:
            rejected.append(_rejection(idx, None, [parse_err]))
            continue

        result = _compile(spec, ctx, seen_hashes)
        if not result.ok or result.plan is None:
            rejected.append(_rejection(
                idx, spec.id,
                [{"code": e.code, "path": e.path, "message": e.message}
                 for e in result.errors],
                duplicate_of=result.duplicate_of))
            continue

        # Reserve the hash for the rest of THIS batch. The compiler cannot see
        # siblings submitted in the same call, so two identical specs in one
        # payload would both compile cleanly without this.
        seen_hashes[result.plan.spec_hash] = spec.id

        written = await _insert(ex, spec, item, result, agent_version_id)
        accepted.append({
            "index": idx,
            "hypothesis_id": spec.id,
            "version": spec.version,
            "spec_hash": result.plan.spec_hash,
            "data_tier": result.plan.data_tier,
            "holdout": result.plan.holdout,
            "trial_count": result.plan.trial_count,
            "bias_flags": list(result.plan.bias_flags),
            "constraints": list(result.plan.constraints),
            "warnings": list(result.warnings),
            "checks_deferred": list(result.checks_deferred),
            "inserted": written,   # False = already present; a retry is a no-op
        })

    can_repair = bool(rejected) and attempt < MAX_REPAIR_ATTEMPTS
    note = None
    if over_cap:
        note = (f"submitted {len(hypotheses)} but the cap is {MAX_PER_CALL}; "
                f"only the first {MAX_PER_CALL} were considered")

    return envelope("submit_hypotheses", {
        "attempt": attempt,
        "max_repair_attempts": MAX_REPAIR_ATTEMPTS,
        "may_repair": can_repair,
        "accepted": accepted,
        "rejected": rejected,
        "agent_run_id": agent_run_id,
        "trace_id": trace_id,
    }, dataset_version=ctx.dataset_version, note=note)


# ── helpers ──────────────────────────────────────────────────────────────

def _parse_spec(item: Dict[str, Any]) -> Tuple[Optional[HypothesisSpec], Dict[str, Any]]:
    """Validate the envelope shape before the compiler sees it.

    A pydantic failure here is still a repair instruction, so it is shaped like
    a CompileError rather than raised.
    """
    if not isinstance(item, dict) or "spec" not in item:
        return None, {"code": "E000", "path": "item",
                      "message": "each item needs a 'spec' object"}
    try:
        return HypothesisSpec.model_validate(item["spec"]), {}
    except Exception as exc:   # pydantic ValidationError, or anything it raises
        return None, {"code": "E000", "path": "spec",
                      "message": f"spec failed validation: {str(exc)[:400]}"}


def _compile(spec: HypothesisSpec, ctx: CompileContext,
             seen_hashes: Dict[str, str]) -> CompileResult:
    """Compile against a context extended with this batch's own hashes."""
    if not seen_hashes:
        return compile_spec(spec, ctx)
    merged = dict(ctx.known_spec_hashes)
    merged.update(seen_hashes)
    return compile_spec(spec, _with_hashes(ctx, merged))


def _with_hashes(ctx: CompileContext, hashes: Dict[str, str]) -> CompileContext:
    """CompileContext is a plain dataclass; copy it with new hashes."""
    return CompileContext(
        dataset_version=ctx.dataset_version,
        snapshot_age_hours=ctx.snapshot_age_hours,
        source_max_dates=ctx.source_max_dates,
        integrity_flags=ctx.integrity_flags,
        integrity_failures=ctx.integrity_failures,
        sealed_from=ctx.sealed_from,
        embargoes=ctx.embargoes,
        known_spec_hashes=hashes,
        family_trials_remaining=ctx.family_trials_remaining,
        event_counter=ctx.event_counter,
    )


def _rejection(idx: int, hypothesis_id: Optional[str],
               errors: Sequence[Dict[str, Any]],
               duplicate_of: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "index": idx,
        "hypothesis_id": hypothesis_id,
        "errors": [e for e in errors if e],
    }
    if duplicate_of:
        out["duplicate_of"] = duplicate_of
    return out


async def _insert(ex: Executor, spec: HypothesisSpec, item: Dict[str, Any],
                  result: CompileResult, agent_version_id: str) -> bool:
    """Write the hypothesis and its lineage. Returns False if already present.

    ON CONFLICT DO NOTHING on both tables is what makes a replayed submission a
    no-op rather than a duplicate.
    """
    spec_json = spec.model_dump(mode="json", by_alias=True)
    rows = await ex(
        """INSERT INTO factory.hypotheses
             (hypothesis_id, version, family_id, question_id, claim, spec_yaml,
              spec_json, horizon, data_tier, created_by)
           VALUES (:hid, :ver, :family, :question, :claim, '', :spec_json,
                   :horizon, :tier, :created_by)
           ON CONFLICT (hypothesis_id, version) DO NOTHING
           RETURNING hypothesis_id""",
        {
            "hid": spec.id,
            "ver": spec.version,
            "family": spec.family,
            "question": getattr(spec, "question", None),
            "claim": _json(spec.claim.model_dump()),
            "spec_json": _json(spec_json),
            "horizon": spec.horizon,
            "tier": result.plan.data_tier if result.plan else None,
            "created_by": agent_version_id,
        })

    for parent in item.get("parents") or []:
        if not isinstance(parent, dict) or not parent.get("id"):
            continue
        await ex(
            """INSERT INTO factory.hypothesis_lineage
                 (child_id, child_version, parent_id, parent_version, op, diff)
               VALUES (:cid, :cver, :pid, :pver, :op, :diff)
               ON CONFLICT ON CONSTRAINT uq_lineage_edge DO NOTHING""",
            {
                "cid": spec.id,
                "cver": spec.version,
                "pid": parent.get("id"),
                "pver": parent.get("version", 1),
                "op": parent.get("op", "mutation"),
                "diff": _json(parent.get("diff") or {}),
            })

    return bool(rows)


def _json(value: Any) -> str:
    """jsonb columns take a JSON string through asyncpg, never a dict.

    Same class of bug as the pgvector list: the driver has no codec for a
    Python dict, so this conversion belongs at the boundary.
    """
    import json
    return json.dumps(value, default=str)

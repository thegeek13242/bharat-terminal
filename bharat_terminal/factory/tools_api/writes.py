"""Write tools: the five an agent may call.

  submit_hypotheses       Scientist
  submit_attack_plan      Adversary
  submit_allocation       Chief Scientist
  submit_report_narrative Reporter
  report_data_gap         all four


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

The dangerous half of that gap IS now closed. Reusing an id for DIFFERENT
mechanics used to vanish into ON CONFLICT: the insert did nothing, the tool
reported success, and the agent's summary described a hypothesis the ledger did
not contain. That is now E016, checked on spec_hash (mechanics only, so a
reworded claim over identical mechanics is still a replay). Observed live on
2026-09-16 before the guard existed -- run 3 described H-000002 as a 10-day hold
while the ledger held run 2's 20-day spec, and the run still reported
"succeeded".

**Cross-submit deduplication is incomplete.** `ctx.known_spec_hashes` maps
spec_hash -> experiment_id, and an experiment only exists once a cycle runs. A
hypothesis row carries no spec_hash column, so two agents can submit identical
mechanics and both land as hypotheses; they collide later at
`experiments.spec_hash` (UNIQUE) instead of at submit time. Within a single
batch this module DOES dedupe, because that case is free.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bharat_terminal.factory.dsl.compiler import (
    CompileContext,
    CompileResult,
    compile_spec,
)
from bharat_terminal.factory.dsl.hashing import spec_hash
from bharat_terminal.factory.dsl.models import HypothesisSpec
from bharat_terminal.factory.tools_api.envelope import MAX_TEXT_CHARS, envelope
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

        clash = await _id_clash(ex, spec, result.plan.spec_hash)
        if clash:
            rejected.append(_rejection(idx, spec.id, [clash]))
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


async def _suggest_free_id(ex: Executor) -> str:
    """A concrete id the caller can retry with.

    "Choose an unused id" is not actionable by something that cannot enumerate
    the used ones, so the error names one. Advisory: two concurrent submitters
    can be handed the same id, and the second still gets E016 -- which is the
    correct outcome, just one round-trip later.
    """
    try:
        rows = await ex(
            """SELECT COALESCE(MAX(CAST(SUBSTRING(hypothesis_id FROM 3) AS INTEGER)), 0) AS mx
                 FROM factory.hypotheses
                WHERE hypothesis_id ~ '^H-[0-9]+$'""", {})
        return f"H-{int(rows[0]['mx']) + 1:06d}"
    except Exception:
        return "an id above the highest currently in the ledger"


async def _id_clash(ex: Executor, spec: HypothesisSpec,
                    new_hash: str) -> Optional[Dict[str, Any]]:
    """Refuse to reuse an id for different work.

    ON CONFLICT DO NOTHING is correct for a REPLAYED identical payload and
    actively wrong for a different payload under the same id: the insert
    silently vanishes, the tool reports success, and the agent's own summary
    then describes a hypothesis the ledger does not contain. That happened on
    2026-09-16 -- run 3 described H-000002 as a 10-day hold while the ledger
    held the 20-day spec from run 2, and the run still reported "succeeded".

    The comparison is on spec_hash, which covers mechanics only, so a reworded
    claim over identical mechanics is still a replay, while different mechanics
    under a reused id is a collision.
    """
    rows = await ex(
        """SELECT spec_json FROM factory.hypotheses
            WHERE hypothesis_id = :hid AND version = :ver""",
        {"hid": spec.id, "ver": spec.version})
    if not rows:
        return None
    raw = rows[0].get("spec_json")
    stored = json.loads(raw) if isinstance(raw, str) else raw
    try:
        stored_hash = spec_hash(HypothesisSpec.model_validate(stored))
    except Exception:
        return {"code": "E016", "path": "id",
                "message": (f"{spec.id} v{spec.version} already exists and its stored "
                            f"spec could not be re-read. Resubmit as "
                            f"{await _suggest_free_id(ex)}.")}
    if stored_hash == new_hash:
        return None                      # genuine idempotent replay
    return {"code": "E016", "path": "id",
            "message": (f"{spec.id} v{spec.version} already exists with DIFFERENT "
                        f"mechanics (stored {stored_hash[:12]}, proposed "
                        f"{new_hash[:12]}). A hypothesis id is immutable. Resubmit "
                        f"this spec as {await _suggest_free_id(ex)} without changing "
                        f"its mechanics, or submit as a new version of {spec.id}.")}


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


# ── report_data_gap ──────────────────────────────────────────────────────
#
# The safe form of "let agents build their own tools". An agent that cannot
# see what it needs DECLARES the gap; a human turns it into a tool or a
# feature PR. No code is generated and nothing is executed.
#
# The queue is the evidence: if reports cluster around one theme, that is a
# better trigger for building a tool than an agent's in-the-moment judgement.
# Which is exactly why this dedupes -- ten identical rows from one retrying
# agent is noise that would drown the signal it exists to produce.

GAP_SOURCE = "agent_gap"          # 9 chars; research_questions.source is String(16)
GAP_STATUS_OPEN = "open"
MIN_NEED_CHARS = 20               # a crude but honest proxy for "concrete"
MAX_ID_ALLOCATION_RETRIES = 3


async def report_data_gap(
    ex: Executor,
    need: str,
    *,
    agent_version_id: str,
    family_id: Optional[str] = None,
    blocked_tool: Optional[str] = None,
    suggested_source: Optional[str] = None,
    evidence_refs: Sequence[Any] = (),
    agent_run_id: Optional[int] = None,
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Record that an agent could not see something it needed.

    Returns `{question_id, created}`. `created=False` means an identical open
    gap already existed and this call added nothing -- which is the correct
    outcome for a retry, not a failure.
    """
    text = (need or "").strip()
    if len(text) < MIN_NEED_CHARS:
        return envelope("report_data_gap", None, note=(
            f"a gap report must name a concrete need of at least "
            f"{MIN_NEED_CHARS} characters; got {len(text)}"))
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]

    # research_questions.family_id is a FOREIGN KEY to research_families.
    # Checking it here turns an FK violation -- which would surface as an
    # exception through the Gateway -- into a repairable message.
    if family_id:
        known = await ex(
            "SELECT family_id FROM factory.research_families WHERE family_id = :fid",
            {"fid": family_id})
        if not known:
            return envelope("report_data_gap", None, note=(
                f"unknown family_id '{family_id}'. Omit it, or use one from "
                f"get_family_state."))

    fingerprint = _normalise(text)
    existing = await ex(
        """SELECT question_id FROM factory.research_questions
            WHERE source = :source AND status = :status
              AND lower(regexp_replace(text, '\\s+', ' ', 'g')) = :fp
            LIMIT 1""",
        {"source": GAP_SOURCE, "status": GAP_STATUS_OPEN, "fp": fingerprint})
    if existing:
        return envelope("report_data_gap", {
            "question_id": existing[0]["question_id"],
            "created": False,
        }, note="an identical open gap already exists; nothing was added")

    refs = {
        "reported_by": agent_version_id,
        "blocked_tool": blocked_tool,
        "blocked_tool_known": _is_known_tool(blocked_tool),
        "suggested_source": suggested_source,
        "agent_run_id": agent_run_id,
        "trace_id": trace_id,
        "refs": list(evidence_refs),
    }

    # Two concurrent reports can compute the same next id; ON CONFLICT makes
    # the loser insert nothing, so it recomputes rather than raising.
    for _ in range(MAX_ID_ALLOCATION_RETRIES):
        qid = await _next_question_id(ex)
        rows = await ex(
            """INSERT INTO factory.research_questions
                 (question_id, family_id, text, source, status, evidence_refs)
               VALUES (:qid, :fid, :text, :source, :status, :refs)
               ON CONFLICT (question_id) DO NOTHING
               RETURNING question_id""",
            {"qid": qid, "fid": family_id, "text": text,
             "source": GAP_SOURCE, "status": GAP_STATUS_OPEN,
             "refs": _json(refs)})
        if rows:
            return envelope("report_data_gap",
                            {"question_id": qid, "created": True},
                            note="a human decides whether this becomes a tool "
                                 "or a feature PR")
    return envelope("report_data_gap", None,
                    note="could not allocate a question id; try again")


def _normalise(text: str) -> str:
    """Fold whitespace and case so trivial rewordings still dedupe."""
    import re
    return re.sub(r"\s+", " ", text).strip().lower()


async def _next_question_id(ex: Executor) -> str:
    """Allocate the next RQ-nnnnnn.

    research_questions.question_id has no default and no sequence, so the id
    is derived from the current maximum. The regex guard keeps a
    differently-shaped id (an import, say) from breaking the cast.
    """
    rows = await ex(
        """SELECT COALESCE(MAX((substring(question_id from '^RQ-([0-9]+)$'))::int), 0) + 1
                  AS next_n
             FROM factory.research_questions
            WHERE question_id ~ '^RQ-[0-9]+$'""",
        {})
    n = (rows[0]["next_n"] if rows and rows[0].get("next_n") else 1)
    return f"RQ-{int(n):06d}"


def _is_known_tool(name: Optional[str]) -> Optional[bool]:
    """Is `blocked_tool` a tool that actually exists?

    Recorded rather than enforced. An agent naming a tool that does not exist
    is itself informative -- it may be describing the tool it wishes it had.
    """
    if not name:
        return None
    from bharat_terminal.factory.tools_api import reads
    known = {n for n in dir(reads) if n.startswith(("get_", "search_", "list_"))}
    return name in known or f"get_{name}" in known


# ── column ceilings ──────────────────────────────────────────────────────
#
# Verified against the live schema 2026-09-14. These are narrow enough to
# reject realistic values, and a silent truncation in an audit ledger is worse
# than a loud refusal -- so they are named here rather than discovered in
# production.
MAX_EXPERIMENT_ID = 16       # robustness_tests.experiment_id
MAX_REQUESTED_BY = 16        # robustness_tests.requested_by
MAX_VERDICT = 16             # robustness_tests.verdict
VERDICT_REQUESTED = "requested"

MAX_ATTACKS_PER_PLAN = 8
MAX_MEMO_CHARS = 4_000


async def submit_attack_plan(
    ex: Executor,
    experiment_id: str,
    attacks: Sequence[Dict[str, Any]],
    kill_memo: str,
    *,
    agent_version_id: str,
    agent_run_id: Optional[int] = None,
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The Adversary's only write: request catalogue attacks and file a memo.

    An attack not in the catalogue is refused -- an agent may compose the
    battery, never invent it. The memo must cite at least one real run_id; an
    uncited memo is rejected rather than stored, because a kill argument that
    references nothing is rhetoric.

    The memo is ADVISORY. A veto is upheld only when a critical test actually
    fails, and that check is deterministic in `gates.engine`. Nothing here can
    change a verdict: rows are written with verdict='requested'.

    SCHEMA GAP, named rather than worked around: there is no `attack_plans`
    table among the 25, and `gate_decisions.llm_recommendation` -- where a memo
    belongs -- is writable only by `factory_gate`. So the memo is carried in
    `robustness_tests.details` on each row of the plan, keyed by a shared
    plan_id. A dedicated table would need a migration.
    """
    from bharat_terminal.factory.tools_api.catalogue import BY_ID, is_known

    if len(experiment_id or "") > MAX_EXPERIMENT_ID:
        return envelope("submit_attack_plan", None, note=(
            f"experiment_id exceeds {MAX_EXPERIMENT_ID} characters"))

    memo = (kill_memo or "").strip()[:MAX_MEMO_CHARS]
    if not memo:
        return envelope("submit_attack_plan", None,
                        note="a kill memo is required")

    runs = await ex(
        """SELECT run_id FROM factory.experiment_runs
            WHERE experiment_id = :eid ORDER BY run_id""",
        {"eid": experiment_id})
    real_run_ids = {str(r["run_id"]) for r in runs}
    if not real_run_ids:
        return envelope("submit_attack_plan", None, note=(
            f"no runs exist for {experiment_id}; there is nothing to attack"))

    # Word-boundary match, NOT substring. `rid in memo` would let "we ran 15
    # tests" cite run 1 and run 5 -- defeating the whole point of the guard for
    # exactly the small ids a fresh ledger produces. The check exists to stop a
    # memo that references nothing being accepted as evidence.
    import re
    mentioned = set(re.findall(r"\b\d+\b", memo))
    cited = sorted(real_run_ids & mentioned, key=int)
    if not cited:
        return envelope("submit_attack_plan", None, note=(
            "the memo cites no real run_id for this experiment; "
            f"known runs are {sorted(real_run_ids)[:8]}"))

    # A STABLE digest, not Python's hash(): the builtin string hash is
    # randomized per process (PYTHONHASHSEED), so the same memo would produce a
    # different plan_id in every worker and the id linking a plan's rows would
    # be meaningless in an audit ledger.
    import hashlib
    digest = hashlib.sha256(f"{experiment_id}|{memo}".encode()).hexdigest()[:8]
    plan_id = f"AP-{experiment_id}-{digest}"
    accepted: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    seen: set = set()

    for idx, attack in enumerate(list(attacks)[:MAX_ATTACKS_PER_PLAN]):
        test_id = (attack or {}).get("test_id") if isinstance(attack, dict) else None
        if not test_id or not is_known(test_id):
            refused.append({"index": idx, "test_id": test_id,
                            "reason": "not in the battery catalogue"})
            continue
        if test_id in seen:
            refused.append({"index": idx, "test_id": test_id,
                            "reason": "already requested in this plan"})
            continue
        seen.add(test_id)

        spec = BY_ID[test_id]
        await ex(
            """INSERT INTO factory.robustness_tests
                 (experiment_id, battery_test, critical, requested_by, verdict, details)
               VALUES (:eid, :test, :critical, :by, :verdict, :details)""",
            {
                "eid": experiment_id,
                "test": test_id,
                "critical": spec.critical,
                # requested_by is varchar(16); the full id is kept in details so
                # the audit trail survives the truncation rather than losing it.
                "by": agent_version_id[:MAX_REQUESTED_BY],
                "verdict": VERDICT_REQUESTED[:MAX_VERDICT],
                "details": _json({
                    "plan_id": plan_id,
                    "params": (attack.get("params") or {}),
                    "cites": cited,
                    "memo": memo,
                    "requested_by_full": agent_version_id,
                    "agent_run_id": agent_run_id,
                    "trace_id": trace_id,
                }),
            })
        accepted.append({"index": idx, "test_id": test_id,
                         "critical": spec.critical,
                         "kill_condition": spec.kill_condition})

    return envelope("submit_attack_plan", {
        "plan_id": plan_id,
        "experiment_id": experiment_id,
        "cited_run_ids": cited,
        "accepted": accepted,
        "refused": refused,
        "note_on_authority": ("advisory only; a veto is upheld only when a "
                              "critical test fails, checked deterministically"),
    })


async def submit_allocation(
    ex: Executor,
    *,
    agent_version_id: str,
    allocations: Sequence[Dict[str, Any]] = (),
    kills: Sequence[Dict[str, Any]] = (),
    revisits: Sequence[Dict[str, Any]] = (),
    global_cap_usd: Optional[float] = None,
    agent_run_id: Optional[int] = None,
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The Chief Scientist's only write: a frontier snapshot.

    IMPORTANT, and a deliberate property rather than a limitation: this tool
    RECORDS an allocation, it does not APPLY one. `factory_writer` holds
    INSERT and SELECT and no UPDATE anywhere, so nothing here can set
    `research_families.status` to paused or killed. A kill is a recommendation
    in the snapshot; applying it needs `factory_gate`. An agent therefore
    cannot halt a line of research by itself, which is the intended boundary.

    A revisit is checked with `gates.engine.family_pause_check` -- the same
    deterministic rule the gate uses, not a second implementation of it. Prose
    alone never reopens a family.
    """
    from bharat_terminal.factory.gates.engine import family_pause_check

    applied: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []

    families = await ex(
        "SELECT family_id, status, budget_cap_usd FROM factory.research_families", {})
    known = {f["family_id"]: f for f in families}

    total = 0.0
    for idx, a in enumerate(allocations):
        fid = (a or {}).get("family_id")
        usd = float((a or {}).get("budget_usd") or 0)
        if fid not in known:
            refused.append({"index": idx, "item": "allocation", "family_id": fid,
                            "reason": "unknown family_id"})
            continue
        if usd < 0:
            refused.append({"index": idx, "item": "allocation", "family_id": fid,
                            "reason": "budget must not be negative"})
            continue
        total += usd
        applied.append({"item": "allocation", "family_id": fid, "budget_usd": usd})

    if global_cap_usd is not None and total > float(global_cap_usd):
        return envelope("submit_allocation", None, note=(
            f"allocations total ${total:,.2f}, over the ${float(global_cap_usd):,.2f} "
            f"global cap; nothing was recorded"))

    for idx, k in enumerate(kills):
        fid = (k or {}).get("family_id")
        reason = ((k or {}).get("reason") or "").strip()
        if fid not in known:
            refused.append({"index": idx, "item": "kill", "family_id": fid,
                            "reason": "unknown family_id"})
        elif not reason:
            refused.append({"index": idx, "item": "kill", "family_id": fid,
                            "reason": "a kill must state a reason"})
        else:
            applied.append({"item": "kill", "family_id": fid, "reason": reason,
                            "status": "recommended_not_applied"})

    for idx, r in enumerate(revisits):
        fid = (r or {}).get("family_id")
        refs = list((r or {}).get("evidence_refs") or [])
        fails = int((r or {}).get("consecutive_failures") or 0)
        if fid not in known:
            refused.append({"index": idx, "item": "revisit", "family_id": fid,
                            "reason": "unknown family_id"})
            continue
        should_pause, why = family_pause_check(fails, refs)
        if should_pause:
            refused.append({"index": idx, "item": "revisit", "family_id": fid,
                            "reason": why})
        else:
            applied.append({"item": "revisit", "family_id": fid,
                            "evidence_refs": refs, "rationale": why})

    rows = await ex(
        """INSERT INTO factory.frontier_snapshots (ranking, allocation)
           VALUES (:ranking, :allocation)
           RETURNING snapshot_id""",
        {
            "ranking": _json([a for a in applied if a["item"] == "allocation"]),
            "allocation": _json({
                "applied": applied,
                "refused": refused,
                "total_usd": total,
                "global_cap_usd": global_cap_usd,
                "submitted_by": agent_version_id,
                "agent_run_id": agent_run_id,
                "trace_id": trace_id,
            }),
        })

    return envelope("submit_allocation", {
        "snapshot_id": rows[0]["snapshot_id"] if rows else None,
        "applied": applied,
        "refused": refused,
        "total_usd": total,
        "note_on_authority": ("recorded, not applied; status changes require "
                              "factory_gate"),
    })


async def submit_report_narrative(
    ex: Executor,
    report_date: Any,
    sections: Dict[str, str],
    *,
    agent_version_id: str,
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The Reporter's only write.

    FACTS ARE RE-DERIVED HERE, NEVER ACCEPTED FROM THE AGENT. If the Reporter
    supplied its own facts payload it could pass fabricated numbers and its
    narrative would reconcile cleanly against them, defeating the entire
    guarantee. So the facts come from the ledger and the narrative is checked
    against those.

    Sentences with unbacked numbers, unknown IDs or overclaims are DROPPED, not
    rejected -- a partly-reconciled report still ships, because a silent
    missing report is worse than a shorter one.

    `daily_research_reports` has PK report_date and no UPDATE grant, so the row
    is written once. A second call for the same date is a no-op; a narrative
    cannot be revised, which is worth knowing before relying on it.
    """
    from bharat_terminal.factory.report.reconcile import reconcile_narrative
    from bharat_terminal.factory.tools_api.reads import get_report_facts

    facts_env = await get_report_facts(ex, report_date)
    facts = facts_env.get("data") or {}
    numbers = facts.get("numbers") or []
    identifiers = facts.get("identifiers") or []

    kept_sections: Dict[str, str] = {}
    dropped: List[Dict[str, Any]] = []
    for name, text in (sections or {}).items():
        result = reconcile_narrative(str(text or ""), numbers, identifiers)
        kept_sections[name] = result.text
        for sentence, reason in result.dropped:
            dropped.append({"section": name, "sentence": sentence, "reason": reason})

    claim_check = {
        "dropped": dropped,
        "kept_sections": {k: len(v) for k, v in kept_sections.items()},
        "author": agent_version_id,
    }
    body = "\n\n".join(f"{k}\n{v}" for k, v in kept_sections.items() if v)

    rows = await ex(
        """INSERT INTO factory.daily_research_reports
             (report_date, status, facts, text, claim_check, trace_id)
           VALUES (:d, 'draft', :facts, :text, :claim_check, :trace)
           ON CONFLICT (report_date) DO NOTHING
           RETURNING report_date""",
        {"d": report_date, "facts": _json(facts), "text": body,
         "claim_check": _json(claim_check), "trace": trace_id})

    return envelope("submit_report_narrative", {
        "report_date": report_date,
        "written": bool(rows),
        "kept": sum(1 for v in kept_sections.values() if v),
        "dropped": dropped,
        "dropped_count": len(dropped),
    }, note=None if rows else "a report for this date already exists; not overwritten")

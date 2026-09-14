"""The sixteen read tools.

Every tool is a coroutine taking an injected `Executor` as its first argument
and returning an envelope. Nothing here opens a connection -- see
`tools_api.pools` for the real adapter, which tests never construct.

Two tools take no executor at all (`list_features`, `get_battery_catalog`):
they read code, because a feature and an attack are code artifacts that arrive
by pull request, never by agent write.

`get_paper_feedback` is the ONLY tool that needs the reader pool. factory_writer
holds zero grants on `public`, so the other fifteen are structurally unable to
reach production data -- that is enforcement, not convention.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from bharat_terminal.factory.registry.features import (
    CONTROL_COVERAGE_FLOOR,
    REGISTRY,
)
from bharat_terminal.factory.tools_api.catalogue import battery_catalog
from bharat_terminal.factory.tools_api.envelope import (
    MAX_ROWS,
    envelope,
    truncate,
    untrusted,
)

# (sql, params) -> rows. Deliberately the narrowest thing that works, so a test
# double is three lines and a tool can never reach past it.
Executor = Callable[[str, Dict[str, Any]], Awaitable[List[Dict[str, Any]]]]

# search_memory embeds its query. The embedder is injected for the same reason
# the executor is: an LLM call inside a read tool would make the test suite
# depend on a network.
Embedder = Callable[[str], Awaitable[Sequence[float]]]

EMBEDDING_COL = "embedding"


def to_pgvector(values: Sequence[float]) -> str:
    """Render an embedding as a pgvector literal.

    asyncpg has no codec registered for the `vector` type, so a Python list
    reaches the driver as an unknown and it raises "expected str, got list".
    pgvector's text input is `[a,b,c]`, so the conversion belongs here at the
    boundary rather than at every call site. Found by running against the real
    database; no fake executor can surface a driver type error.
    """
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


# ── memory ───────────────────────────────────────────────────────────────

async def search_memory(
    ex: Executor,
    query: str,
    *,
    embedder: Embedder,
    k: int = 8,
    family_id: Optional[str] = None,
    include_quarantined: bool = False,
) -> Dict[str, Any]:
    """Vector search over `failures`.

    Quarantined failures are EXCLUDED by default. A lesson drawn from a
    leakage-tainted result may read as perfectly sound, so it must not steer
    new work just because it is semantically close.
    """
    k = max(1, min(int(k), 20))
    vec = to_pgvector(await embedder(query))

    where = [f"{EMBEDDING_COL} IS NOT NULL"]
    params: Dict[str, Any] = {"q": vec, "k": k}
    if not include_quarantined:
        where.append("NOT quarantined")
    if family_id:
        where.append("hypothesis_id IN (SELECT hypothesis_id FROM factory.hypotheses "
                     "WHERE family_id = :family_id)")
        params["family_id"] = family_id

    sql = f"""
        SELECT failure_id, hypothesis_id, version, experiment_id, category,
               reason, lesson, recommendation, evidence_refs, provenance,
               quarantined, created_at,
               (1 - ({EMBEDDING_COL} <=> (:q)::vector)) AS similarity
          FROM factory.failures
         WHERE {' AND '.join(where)}
         ORDER BY {EMBEDDING_COL} <=> (:q)::vector
         LIMIT :k
    """
    rows = await ex(sql, params)
    items, trunc = truncate([_failure_projection(r) for r in rows], k)
    return envelope("search_memory",
                    {"query_family": family_id,
                     "include_quarantined": include_quarantined,
                     "results": items},
                    truncated=trunc)


async def get_failure(ex: Executor, failure_id: str) -> Dict[str, Any]:
    """Fetch one failure by id.

    Exists because `context_refs.memory` in a FrontierSlot passes FAIL ids, and
    a semantic search cannot resolve an identifier.
    """
    rows = await ex(
        """SELECT failure_id, hypothesis_id, version, experiment_id, category,
                  reason, lesson, recommendation, evidence_refs, provenance,
                  quarantined, created_at
             FROM factory.failures WHERE failure_id = :failure_id""",
        {"failure_id": failure_id})
    if not rows:
        return envelope("get_failure", None, note=f"no such failure: {failure_id}")
    return envelope("get_failure", _failure_projection(rows[0]))


def _failure_projection(r: Dict[str, Any]) -> Dict[str, Any]:
    """`reason`, `lesson` and `recommendation` are agent-authored. Mark them."""
    return {
        "failure_id": r.get("failure_id"),
        "hypothesis_id": r.get("hypothesis_id"),
        "version": r.get("version"),
        "experiment_id": r.get("experiment_id"),
        "category": r.get("category"),
        "provenance": r.get("provenance"),
        "quarantined": r.get("quarantined"),
        "evidence_refs": r.get("evidence_refs"),
        "created_at": r.get("created_at"),
        "similarity": r.get("similarity"),
        "reason": untrusted(r.get("reason")),
        "lesson": untrusted(r.get("lesson")),
        "recommendation": untrusted(r.get("recommendation")),
    }


# ── hypotheses, questions, families ──────────────────────────────────────

async def get_hypothesis(
    ex: Executor,
    hypothesis_id: str,
    version: Optional[int] = None,
    *,
    include_spec: bool = False,
) -> Dict[str, Any]:
    """Claim, status and lineage. `spec_json` only on request -- it is large."""
    params: Dict[str, Any] = {"hid": hypothesis_id}
    ver = "AND h.version = :version" if version is not None else ""
    if version is not None:
        params["version"] = version

    rows = await ex(
        f"""SELECT h.hypothesis_id, h.version, h.family_id, h.question_id,
                   h.claim, h.horizon, h.data_tier, h.created_by, h.created_at,
                   s.status, s.updated_at AS status_updated_at, h.spec_json
              FROM factory.hypotheses h
              LEFT JOIN factory.hypothesis_status s
                     ON s.hypothesis_id = h.hypothesis_id AND s.version = h.version
             WHERE h.hypothesis_id = :hid {ver}
             ORDER BY h.version DESC LIMIT 1""",
        params)
    if not rows:
        return envelope("get_hypothesis", None,
                        note=f"no such hypothesis: {hypothesis_id}")
    h = rows[0]

    parents = await ex(
        """SELECT parent_id, parent_version, op, diff
             FROM factory.hypothesis_lineage
            WHERE child_id = :hid AND child_version = :ver
            ORDER BY id""",
        {"hid": h["hypothesis_id"], "ver": h["version"]})

    data = {
        "hypothesis_id": h.get("hypothesis_id"),
        "version": h.get("version"),
        "family_id": h.get("family_id"),
        "question_id": h.get("question_id"),
        "horizon": h.get("horizon"),
        "data_tier": h.get("data_tier"),
        "status": h.get("status"),
        "status_updated_at": h.get("status_updated_at"),
        "created_by": h.get("created_by"),
        "created_at": h.get("created_at"),
        # An agent wrote this claim. It is evidence, not instruction.
        "claim": untrusted(_as_text(h.get("claim"))),
        "parents": [{"parent_id": p.get("parent_id"),
                     "parent_version": p.get("parent_version"),
                     "op": p.get("op"),
                     "diff": p.get("diff")} for p in parents],
    }
    if include_spec:
        data["spec_json"] = h.get("spec_json")
    return envelope("get_hypothesis", data)


async def get_research_question(ex: Executor, question_id: str) -> Dict[str, Any]:
    """Resolve the question a Scientist was assigned.

    Without this the agent is handed a `question_id` in its FrontierSlot and
    cannot read what it was asked -- it was the first blocker the workflow
    walkthrough found.
    """
    rows = await ex(
        """SELECT question_id, family_id, text, source, status,
                  evidence_refs, created_at, closed_at
             FROM factory.research_questions WHERE question_id = :qid""",
        {"qid": question_id})
    if not rows:
        return envelope("get_research_question", None,
                        note=f"no such question: {question_id}")
    q = rows[0]
    return envelope("get_research_question", {
        "question_id": q.get("question_id"),
        "family_id": q.get("family_id"),
        "source": q.get("source"),
        "status": q.get("status"),
        "evidence_refs": q.get("evidence_refs"),
        "created_at": q.get("created_at"),
        "closed_at": q.get("closed_at"),
        # Questions raised by agents (source='agent_gap') are agent-authored.
        "text": untrusted(q.get("text")),
    })


async def get_family_state(
    ex: Executor, family_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Family status, budget and the ladder histogram.

    The Chief may omit family_id for all families; Scientist and Adversary are
    expected to name one (enforced at the Gateway, not here).
    """
    params: Dict[str, Any] = {}
    where = ""
    if family_id:
        where = "WHERE f.family_id = :fid"
        params["fid"] = family_id

    fams = await ex(
        f"""SELECT f.family_id, f.title, f.horizon, f.data_tier, f.status,
                   f.budget_cap_usd, f.killed_reason, f.created_at
              FROM factory.research_families f {where}
             ORDER BY f.family_id""",
        params)
    counts = await ex(
        f"""SELECT h.family_id, s.status, count(*) AS n
              FROM factory.hypotheses h
              JOIN factory.hypothesis_status s
                ON s.hypothesis_id = h.hypothesis_id AND s.version = h.version
              {'WHERE h.family_id = :fid' if family_id else ''}
             GROUP BY h.family_id, s.status""",
        params)

    by_family: Dict[str, Dict[str, int]] = {}
    for c in counts:
        by_family.setdefault(c["family_id"], {})[c["status"]] = c["n"]

    items, trunc = truncate([{
        "family_id": f.get("family_id"),
        "title": f.get("title"),
        "horizon": f.get("horizon"),
        "data_tier": f.get("data_tier"),
        "status": f.get("status"),
        "budget_cap_usd": f.get("budget_cap_usd"),
        "killed_reason": f.get("killed_reason"),
        "counts_by_status": by_family.get(f.get("family_id"), {}),
    } for f in fams])
    return envelope("get_family_state", {"families": items}, truncated=trunc)


# ── experiments, runs, gates, holdout ────────────────────────────────────

async def get_run_results(ex: Executor, experiment_id: str) -> Dict[str, Any]:
    """Runs plus their statistical and battery evidence.

    Holdout-window runs are returned only once the gate has actually unsealed
    them; `window_role` makes that visible rather than implicit.
    """
    runs = await ex(
        """SELECT run_id, experiment_id, dataset_version, feature_versions,
                  code_commit, seed, window_role, status, result, result_hash,
                  cpu_seconds, trace_id, finished_at
             FROM factory.experiment_runs
            WHERE experiment_id = :eid
            ORDER BY run_id""",
        {"eid": experiment_id})
    if not runs:
        return envelope("get_run_results", None,
                        note=f"no runs for experiment: {experiment_id}")

    ids = [r["run_id"] for r in runs]
    stats = await ex(
        """SELECT run_id, kind, output, p_value, passed
             FROM factory.stat_tests WHERE run_id = ANY(:ids) ORDER BY test_id""",
        {"ids": ids})
    battery = await ex(
        """SELECT run_id, battery_test, critical, verdict, requested_by, details
             FROM factory.robustness_tests
            WHERE experiment_id = :eid ORDER BY test_id""",
        {"eid": experiment_id})

    by_run: Dict[Any, List[Dict[str, Any]]] = {}
    for s in stats:
        by_run.setdefault(s["run_id"], []).append(
            {"kind": s.get("kind"), "p_value": s.get("p_value"),
             "passed": s.get("passed"), "output": s.get("output")})

    items, trunc = truncate([{
        "run_id": r.get("run_id"),
        "dataset_version": r.get("dataset_version"),
        "code_commit": r.get("code_commit"),
        "feature_versions": r.get("feature_versions"),
        "seed": r.get("seed"),
        "window_role": r.get("window_role"),
        "status": r.get("status"),
        "result": r.get("result"),
        "result_hash": r.get("result_hash"),
        "cpu_seconds": r.get("cpu_seconds"),
        "trace_id": r.get("trace_id"),
        "finished_at": r.get("finished_at"),
        "stat_tests": by_run.get(r.get("run_id"), []),
    } for r in runs])

    return envelope("get_run_results", {
        "experiment_id": experiment_id,
        "runs": items,
        "robustness": [{"run_id": b.get("run_id"),
                        "battery_test": b.get("battery_test"),
                        "critical": b.get("critical"),
                        "verdict": b.get("verdict"),
                        "requested_by": b.get("requested_by"),
                        "details": b.get("details")} for b in battery],
    }, truncated=trunc)


async def get_gate_decision(
    ex: Executor, hypothesis_id: str, version: Optional[int] = None,
) -> Dict[str, Any]:
    """The actionable "why blocked".

    `rule_results` carries {rule, value, threshold, pass} per rule. Without it
    an agent sees only that a sibling failed, not that DSR was 0.91 against a
    0.95 threshold -- the difference between an informed repair and a guess.
    """
    params: Dict[str, Any] = {"hid": hypothesis_id}
    ver = "AND version = :version" if version is not None else ""
    if version is not None:
        params["version"] = version
    rows = await ex(
        f"""SELECT decision_id, hypothesis_id, version, from_status, to_status,
                   gate_version, rule_results, decided_by, trace_id, decided_at
              FROM factory.gate_decisions
             WHERE hypothesis_id = :hid {ver}
             ORDER BY decided_at DESC LIMIT :lim""",
        {**params, "lim": MAX_ROWS})
    if not rows:
        return envelope("get_gate_decision", None,
                        note=f"no gate decisions for {hypothesis_id}")
    decisions = [{
        "decision_id": d.get("decision_id"),
        "version": d.get("version"),
        "from_status": d.get("from_status"),
        "to_status": d.get("to_status"),
        "gate_version": d.get("gate_version"),
        "decided_by": d.get("decided_by"),
        "decided_at": d.get("decided_at"),
        "trace_id": d.get("trace_id"),
        "rule_results": d.get("rule_results"),
        "blocking": _blocking_rules(d.get("rule_results")),
    } for d in rows]
    return envelope("get_gate_decision",
                    {"hypothesis_id": hypothesis_id, "decisions": decisions})


def _blocking_rules(rule_results: Any) -> List[Dict[str, Any]]:
    """Surface the failed rules directly; an agent should not have to filter."""
    if not isinstance(rule_results, list):
        return []
    return [r for r in rule_results
            if isinstance(r, dict) and r.get("pass") is False]


async def get_holdout_status(ex: Executor, hypothesis_id: str) -> Dict[str, Any]:
    """Whether the single permitted holdout access has been spent.

    "One holdout access" is an anti-overfitting control. An Adversary that
    cannot see the log cannot argue about it.
    """
    rows = await ex(
        """SELECT access_id, hypothesis_id, version, run_id, "window",
                  decision_id, accessed_at
             FROM factory.holdout_access_log
            WHERE hypothesis_id = :hid ORDER BY accessed_at""",
        {"hid": hypothesis_id})
    accesses = [{"access_id": r.get("access_id"),
                 "version": r.get("version"),
                 "run_id": r.get("run_id"),
                 "window": r.get("window"),
                 "decision_id": r.get("decision_id"),
                 "accessed_at": r.get("accessed_at")} for r in rows]
    return envelope("get_holdout_status", {
        "hypothesis_id": hypothesis_id,
        "accesses": accesses,
        "n_accesses": len(accesses),
        "spent": len(accesses) > 0,
    })


# ── datasets, frontier, budget ───────────────────────────────────────────

async def get_dataset_catalog(ex: Executor, limit: int = 5) -> Dict[str, Any]:
    """Snapshot versions with freshness and integrity.

    A stale snapshot surfaces here so an agent can see the compiler will fail
    closed before it spends a turn composing something unrunnable.
    """
    rows = await ex(
        """SELECT dataset_version, manifest, integrity, snapshot_path,
                  sealed_from, embargoes, created_at
             FROM factory.dataset_versions
            ORDER BY created_at DESC LIMIT :lim""",
        {"lim": max(1, min(int(limit), MAX_ROWS))})
    items = [{
        "dataset_version": r.get("dataset_version"),
        "created_at": r.get("created_at"),
        "sealed_from": r.get("sealed_from"),
        "embargoes": r.get("embargoes"),
        "integrity": r.get("integrity"),
        "tables": _manifest_tables(r.get("manifest")),
    } for r in rows]
    return envelope("get_dataset_catalog", {"datasets": items},
                    dataset_version=items[0]["dataset_version"] if items else None)


def _manifest_tables(manifest: Any) -> Dict[str, Any]:
    """Project rows/max_date per table; sha256 and paths are not agent business."""
    if not isinstance(manifest, dict):
        return {}
    tables = manifest.get("tables")
    if not isinstance(tables, dict):
        return {}
    return {name: {"rows": t.get("rows"), "max_date": t.get("max_date")}
            for name, t in tables.items() if isinstance(t, dict)}


async def get_frontier(ex: Executor) -> Dict[str, Any]:
    """Latest ranking and allocation, plus remaining alpha wealth."""
    snap = await ex(
        """SELECT snapshot_id, ranking, allocation, memo_agent_run_id, created_at
             FROM factory.frontier_snapshots ORDER BY created_at DESC LIMIT 1""",
        {})
    wealth = await ex(
        """SELECT wealth_after, created_at FROM factory.alpha_wealth_ledger
            ORDER BY entry_id DESC LIMIT 1""", {})
    s = snap[0] if snap else None
    return envelope("get_frontier", {
        "snapshot_id": s.get("snapshot_id") if s else None,
        "created_at": s.get("created_at") if s else None,
        "ranking": s.get("ranking") if s else [],
        "allocation": s.get("allocation") if s else {},
        "memo_agent_run_id": s.get("memo_agent_run_id") if s else None,
        "alpha_wealth_remaining": wealth[0].get("wealth_after") if wealth else None,
    })


async def get_budget(ex: Executor, days: int = 30) -> Dict[str, Any]:
    """Spend by category and family over a window."""
    days = max(1, min(int(days), 365))
    rows = await ex(
        # make_interval takes an integer directly. The earlier
        # `(:days || ' days')::interval` made Postgres infer $1 as TEXT because
        # of the concatenation, so binding an int raised "expected str, got
        # int" -- a real-database failure invisible to a fake executor.
        """SELECT category, family_id, sum(amount_usd) AS spend_usd, count(*) AS n
             FROM factory.budget_ledger
            WHERE date >= (CURRENT_DATE - make_interval(days => :days))
            GROUP BY category, family_id ORDER BY 3 DESC""",
        {"days": days})
    caps = await ex(
        """SELECT family_id, budget_cap_usd FROM factory.research_families
            WHERE budget_cap_usd IS NOT NULL""", {})
    items, trunc = truncate([{
        "category": r.get("category"),
        "family_id": r.get("family_id"),
        "spend_usd": r.get("spend_usd"),
        "entries": r.get("n"),
    } for r in rows])
    return envelope("get_budget", {
        "window_days": days,
        "spend": items,
        "total_usd": sum(float(r.get("spend_usd") or 0) for r in rows),
        "family_caps": {c["family_id"]: c["budget_cap_usd"] for c in caps},
    }, truncated=trunc)


# ── divergence + paper (reader pool) ─────────────────────────────────────

async def get_divergence_report(ex: Executor, report_id: Any) -> Dict[str, Any]:
    """One divergence report by id.

    `context_refs.divergence` passes report ids; get_paper_feedback filters by
    strategy and date and cannot resolve one.
    """
    rows = await ex(
        """SELECT report_id, strategy_id, version, "window", metrics,
                  classification, evidence, action, followup_question_id, created_at
             FROM factory.divergence_reports WHERE report_id = :rid""",
        {"rid": report_id})
    if not rows:
        return envelope("get_divergence_report", None,
                        note=f"no such report: {report_id}")
    d = rows[0]
    return envelope("get_divergence_report", {
        "report_id": d.get("report_id"),
        "strategy_id": d.get("strategy_id"),
        "version": d.get("version"),
        "window": d.get("window"),
        "metrics": d.get("metrics"),
        "classification": d.get("classification"),
        "action": d.get("action"),
        "followup_question_id": d.get("followup_question_id"),
        "evidence": d.get("evidence"),
        "created_at": d.get("created_at"),
    })


async def get_paper_feedback(
    ex_ro: Executor,
    strategy_id: Optional[str] = None,
    since: Optional[date] = None,
) -> Dict[str, Any]:
    """Divergence classifications joined to realized paper outcomes.

    THE ONLY TOOL THAT TOUCHES PRODUCTION TABLES, so it takes the READER
    executor (factory_api_ro). Passing the writer executor here raises rather
    than silently returning nothing, because a permission error at 3am is
    harder to read than an explicit one.
    """
    params: Dict[str, Any] = {}
    where = []
    if strategy_id:
        where.append("d.strategy_id = :sid")
        params["sid"] = strategy_id
    if since:
        where.append("d.created_at >= :since")
        params["since"] = since
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    reports = await ex_ro(
        f"""SELECT d.report_id, d.strategy_id, d.version, d."window", d.metrics,
                   d.classification, d.action, d.followup_question_id, d.created_at
              FROM factory.divergence_reports d {clause}
             ORDER BY d.created_at DESC LIMIT :lim""",
        {**params, "lim": MAX_ROWS})

    # Column names and the status vocabulary are VERIFIED against the live
    # table (2026-09-14), not inferred: there is no pnl_pct and no
    # slippage_bps_entry, and status is UPPERCASE. An earlier draft filtered
    # on status='closed', which matches zero of 427 rows -- it would have
    # reported "no paper activity" forever instead of failing.
    trades = await ex_ro(
        """SELECT strategy,
                  count(*) AS n_closed,
                  avg(pnl) AS mean_pnl,
                  avg(CASE WHEN entry_price > 0 AND qty > 0
                           THEN pnl / (entry_price * qty) END) AS mean_return,
                  min(exit_at) AS first_exit,
                  max(exit_at) AS last_exit
             FROM public.paper_trades
            WHERE status = 'CLOSED'
              AND (:sid::text IS NULL OR strategy = :sid)
            GROUP BY strategy
            ORDER BY strategy""",
        {"sid": strategy_id})

    items, trunc = truncate([{
        "report_id": r.get("report_id"),
        "strategy_id": r.get("strategy_id"),
        "version": r.get("version"),
        "window": r.get("window"),
        "metrics": r.get("metrics"),
        "classification": r.get("classification"),
        "action": r.get("action"),
        "followup_question_id": r.get("followup_question_id"),
        "created_at": r.get("created_at"),
    } for r in reports])

    return envelope("get_paper_feedback", {
        "divergence_reports": items,
        "realized": [{"strategy": t.get("strategy"),
                      "n_closed": t.get("n_closed"),
                      "mean_pnl": t.get("mean_pnl"),
                      "mean_return": t.get("mean_return"),
                      "first_exit": t.get("first_exit"),
                      "last_exit": t.get("last_exit")}
                     for t in trades],
    }, truncated=trunc)


# ── code-backed tools (no executor) ──────────────────────────────────────

def list_features() -> Dict[str, Any]:
    """The feature registry, from code.

    UNMEASURED coverage is rendered explicitly rather than omitted. Eleven of
    twenty features have no measured coverage, and the compiler treats that as
    a hard failure for controls -- an agent that cannot see it will keep
    proposing controls the compiler will reject.
    """
    feats = []
    for ref, f in sorted(REGISTRY.items()):
        cov = f.coverage
        feats.append({
            "ref": ref,
            "data_tier": f.data_tier,
            "source_table": f.source_table,
            "history_start": f.history_start,
            "value_kind": f.value_kind,
            "valid_clocks": list(f.valid_clocks),
            "control_eligible": f.control_eligible,
            "integrity_flags": list(f.integrity_flags),
            "coverage": cov,
            "coverage_note": (
                "UNMEASURED - cannot be used as a control" if cov is None
                else ("below the control floor "
                      f"({cov:.3f} < {CONTROL_COVERAGE_FLOOR})" if
                      f.control_eligible and cov < CONTROL_COVERAGE_FLOOR
                      else None)),
        })
    return envelope("list_features", {
        "control_coverage_floor": CONTROL_COVERAGE_FLOOR,
        "n_features": len(feats),
        "n_unmeasured": sum(1 for f in feats if f["coverage"] is None),
        "features": feats,
    })


def get_battery_catalog() -> Dict[str, Any]:
    """The fifteen catalogue attacks. An agent may not invent one."""
    return envelope("get_battery_catalog", battery_catalog())


# ── report facts ─────────────────────────────────────────────────────────

async def get_report_facts(ex: Executor, report_date: date) -> Dict[str, Any]:
    """The Reporter's only tool.

    Returns structured facts assembled by `report/facts.py`. The Reporter may
    not introduce a number absent from this payload -- `report/reconcile.py`
    drops any sentence that does.
    """
    from bharat_terminal.factory.report.facts import build_facts

    cycles = await ex(
        """SELECT g.hypothesis_id, g.version, g.to_status,
                  (g.to_status <> g.from_status) AS promoted
             FROM factory.gate_decisions g
            WHERE g.decided_at::date = :d
            ORDER BY g.decision_id""",
        {"d": report_date})
    failures = await ex(
        """SELECT failure_id, hypothesis_id, category, reason, lesson,
                  recommendation
             FROM factory.failures WHERE created_at::date = :d""",
        {"d": report_date})
    ds = await ex(
        """SELECT dataset_version, integrity FROM factory.dataset_versions
            ORDER BY created_at DESC LIMIT 1""", {})
    spend = await ex(
        """SELECT category, sum(amount_usd) AS usd FROM factory.budget_ledger
            WHERE date = :d GROUP BY category""",
        {"d": report_date})

    by_cat = {s["category"]: float(s["usd"] or 0) for s in spend}
    facts = build_facts(
        report_date,
        cycles=[dict(c) for c in cycles],
        failures=[dict(f) for f in failures],
        dataset_version=ds[0]["dataset_version"] if ds else None,
        llm_usd=by_cat.get("llm", 0.0),
        eval_usd=by_cat.get("eval", 0.0),
    )
    return envelope("get_report_facts", {
        "report_date": facts.report_date,
        "dataset_version": facts.dataset_version,
        "scorecard": facts.scorecard.__dict__,
        "failures": facts.failures,
        "activity_low": facts.activity_low,
        "action_required": facts.action_required,
        "actions": facts.actions,
        "numbers": facts.numbers(),
        "identifiers": sorted(facts.identifiers()),
    }, dataset_version=facts.dataset_version)


def _as_text(value: Any) -> Optional[str]:
    """claim is jsonb; render it for the untrusted wrapper without guessing."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = [f"{k}: {v}" for k, v in value.items()]
        return " | ".join(parts)
    return str(value)

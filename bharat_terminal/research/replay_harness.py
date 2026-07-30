"""Tier-B replay harness — paired v2/v3 rubric-selection replay (exp 'RB').

Measures what flipping ROUTER_V2_RUBRICS (router_v2 Tier B: taxonomy-v3-driven
scenario-rubric selection) would have done to stage-3 outputs, on the lab's
trade-space cache (dataset_400d_v3), scored against CAPTURABLE labels — per
SPECIALIST_TREE_ARCHITECTURE_2026-07-28.md §8.3 power rules and
ANALYST_CLONE_ARCHITECTURE_2026-07-29.md §5.

Design (three phases):
  1. POPULATION — hop-0 cache rows; for each row compute BOTH scenario
     compositions: v2 (scenario_rubrics.detect_scenario + rubric + lender
     body-signature addendum — byte-identical to stage-3's flag-off path) and
     v3 (router_v2.scenario_block_v3 under the flag, with company_type-driven
     addenda; company_type/classification fetched for all symbols in ONE DB
     query). AFFECTED = any difference in (scenario_key, prompt block).
  2. SAMPLE — all affected rows, capped (default 1,000) via seed-42 stratified
     sampling by difference-class (proportional, min 30 per class where
     available). Full filing bodies re-fetched from news_items (the cache caps
     body at 8K; stage-3 reads up to IMPACT_BODY_CHARS=120K).
  3. REPLAY — per sampled row × arm {v2, v3}: the REAL stage-3 prompt via
     stage3_impact.build_impact_prompt (same discipline block, same Tier-A
     SEBI/type/state lines in BOTH arms; company context fetched from
     kb-service HTTP exactly as stage-3 does, once per symbol, shared by both
     arms) — the arms differ ONLY in Tier-B rubric selection, toggled through
     the call-time ROUTER_V2_RUBRICS read. llm.acomplete with stage-3's model
     (IMPACT_MODEL) + forced tool, temperature 0, bounded concurrency,
     retry-once on parse failure.

Grounding caveat, stated up front: company context is CURRENT-state (live KB),
not as-of the event — absolute metric levels therefore carry a replay-vs-live
gap, but the context is IDENTICAL across the two arms, so the PAIRED deltas
(the quantities this harness exists to estimate) are unbiased w.r.t. that gap.
Event memory / market backdrop blocks are omitted (also identical-by-absence
across arms). Historical rows predate router-v2, so rows the Tier-A gates
would now suppress (fund schemes, unknown+suspended) are excluded.

Read-only DB (plain SELECTs). Deterministic: seed 42 everywhere, sampling on
sorted keys, no wall-clock in any sampling decision.

Usage (research image, repo mounted over the image, like materiality_lab):
  docker compose run --rm \\
      -v $PWD/bharat_terminal:/app/bharat_terminal:ro -v $PWD/exports:/app/exports \\
      --entrypoint python research-runner -m bharat_terminal.research.replay_harness \\
      --diff-only [--days 400]
  ... --run [--max-n 1000] [--force]

Outputs: exports/materiality_lab/replay_tierb_<date>.json + ledger.jsonl rows
(exp='RB').
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import bharat_terminal.research.materiality_calibrator as mc
import bharat_terminal.research.materiality_lab as ml
from bharat_terminal.analysis import router_v2
from bharat_terminal.analysis.scenario_rubrics import (
    detect_scenario, lender_addendum, rubric_for,
)

logger = logging.getLogger(__name__)

SEED = 42
LAB_DIR = ml.LAB_DIR
GATE_ARTIFACT = f"{LAB_DIR}/materiality_v4_1_tax22.joblib"
HARD_WRONG_PCT = 0.015          # |capturable AR| threshold mirroring the nightly eval
COST_ABORT_USD = 40.0
MIN_PER_CLASS = 30
DEFAULT_CAP = 1000
CONCURRENCY = 5                 # bounded 4-6 per the harness spec
KB_FETCH_ATTEMPTS = 3
BOOTSTRAP_N = 10_000
EST_OUTPUT_TOKENS = 300         # forced-tool single-impact response estimate

# Addendum markers (must match scenario_rubrics.py block headers verbatim).
_HOLDCO_MARK = "INVESTMENT HOLDING COMPANY RESULTS"
_LENDER_MARK = "SECTOR ADDENDUM — LENDER RESULTS"

DIFF_CLASSES = [
    "court_order_reroute", "esop_out_of_fund_raise", "ncd_out_of_fund_raise",
    "approvals", "auditor", "wrapper_content_sniff", "holdco_addendum",
    "lender_addendum_change", "other_diff",
]
FUND_RAISE_CLASSES = ("esop_out_of_fund_raise", "ncd_out_of_fund_raise")


@contextmanager
def _rubrics_flag(on: bool):
    """Flip ROUTER_V2_RUBRICS for the duration of a SYNCHRONOUS block.
    router_v2.rubrics_enabled() reads the env at call time by design, so no
    analysis-code edit is needed; prompt assembly contains no awaits, so the
    flip cannot interleave across asyncio tasks."""
    prev = os.environ.get("ROUTER_V2_RUBRICS")
    os.environ["ROUTER_V2_RUBRICS"] = "1" if on else "0"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("ROUTER_V2_RUBRICS", None)
        else:
            os.environ["ROUTER_V2_RUBRICS"] = prev


# ── Scenario compositions (the two arms) ──────────────────────────────────

def v2_composition(headline: str, body: str) -> Tuple[Optional[str], str]:
    """Legacy arm — byte-identical to stage3._scenario_for's flag-off path."""
    key = detect_scenario(headline or "", body or "")
    block = rubric_for(headline or "", body or "")
    if key:
        block += lender_addendum(key, body or "")
    return key, block


def v3_composition(headline: str, body: str,
                   decision: "router_v2.RouteDecision",
                   addendum_type: Optional[str]) -> Tuple[Optional[str], str]:
    """Tier-B arm — router_v2.scenario_block_v3 under the flag."""
    with _rubrics_flag(True):
        return router_v2.scenario_block_v3(
            headline or "", body or "", decision, company_type=addendum_type)


def classify_diff(key2: Optional[str], block2: str,
                  key3: Optional[str], block3: str,
                  decision: "router_v2.RouteDecision") -> Optional[str]:
    """Difference-class label, or None when the arms are identical."""
    if (key2, block2) == (key3, block3):
        return None
    b = decision.event_bucket
    if key2 == "order_win" and key3 is None and b == "litigation_regulatory":
        return "court_order_reroute"
    if key2 == "fund_raise" and key3 is None and b == "esop_allotment":
        return "esop_out_of_fund_raise"
    if key2 == "fund_raise" and key3 is None and b == "debt_issue":
        return "ncd_out_of_fund_raise"
    if (key2 is None and key3 == "financial_results"
            and "results_content_sniff" in decision.bucket_flags):
        return "wrapper_content_sniff"
    if key2 == key3 == "financial_results":
        if (_HOLDCO_MARK in block3) != (_HOLDCO_MARK in block2):
            return "holdco_addendum"
        if (_LENDER_MARK in block3) != (_LENDER_MARK in block2):
            return "lender_addendum_change"
    if b == "regulatory_approval":
        return "approvals"
    if b == "governance_redflag":
        return "auditor"
    return "other_diff"


# ── DB fetches (plain SELECTs, read-only) ─────────────────────────────────

async def fetch_classifications(symbols: List[str]) -> Dict[str, dict]:
    """company_type/classification for all symbols in ONE query."""
    from sqlalchemy import bindparam, text
    from bharat_terminal.kb.sync.utils import create_async_session

    stmt = text(
        "SELECT symbol, company_type, type_overlays::text AS overlays_json, "
        "is_fund_scheme FROM companies WHERE symbol IN :syms"
    ).bindparams(bindparam("syms", expanding=True))
    SF = create_async_session()
    async with SF() as db:
        rows = (await db.execute(stmt, {"syms": sorted(set(symbols))})).mappings().all()
        await db.rollback()
    out: Dict[str, dict] = {}
    for r in rows:
        try:
            overlays = json.loads(r["overlays_json"] or "[]")
        except (ValueError, TypeError):
            overlays = []
        out[r["symbol"]] = {
            "company_type": r["company_type"],
            "type_overlays": overlays if isinstance(overlays, list) else [],
            "is_fund_scheme": bool(r["is_fund_scheme"]),
        }
    return out


async def fetch_full_bodies(impact_ids: List[str]) -> Dict[str, dict]:
    """Full filing bodies for the sampled impact rows in ONE query."""
    from sqlalchemy import bindparam, text
    from bharat_terminal.kb.sync.utils import create_async_session

    stmt = text(
        "SELECT ir.id AS impact_id, ni.id AS news_id, ni.headline, ni.body, "
        "ni.source, ni.url FROM impact_reports ir "
        "JOIN news_items ni ON ni.id = ir.news_id WHERE ir.id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    SF = create_async_session()
    async with SF() as db:
        rows = (await db.execute(stmt, {"ids": sorted(set(impact_ids))})).mappings().all()
        await db.rollback()
    return {r["impact_id"]: dict(r) for r in rows}


# ── Phase 1: population diff ──────────────────────────────────────────────

def load_frame(days: int):
    """Hop-0 trade-space frame from the lab v3 cache (+ market medians)."""
    import pandas as pd
    df = pd.read_pickle(f"{LAB_DIR}/dataset_{days}d_v3.pkl.gz", compression="gzip")
    mkt = pd.read_pickle(f"{LAB_DIR}/mkt_medians.pkl.gz", compression="gzip")
    d = ml._tradespace_frame(df, mkt)
    d = d[d["hop"] == 0].copy()
    n0 = len(d)
    d = d.drop_duplicates(subset=["impact_id", "symbol"], keep="first")
    if len(d) != n0:
        logger.info("deduped %d duplicate (impact_id, symbol) rows", n0 - len(d))
    d = d.sort_values(["impact_id", "symbol"]).reset_index(drop=True)
    d["n_hop0"] = d.groupby("impact_id")["symbol"].transform("size")
    return d


def compute_population(d, cls_map: Dict[str, dict]):
    """Both routings per hop-0 row (on the CACHED 8K body — sniff/lender
    windows are 40K in production, so sniff-driven classes can undercount
    here; the replay recomputes on full bodies). Adds columns in place."""
    import numpy as np

    buckets, keys2, keys3, classes, suppressed = [], [], [], [], []
    for row in d.itertuples(index=False):
        h = row.headline or ""
        body = row.body or ""
        cls = cls_map.get(row.symbol)
        decision = router_v2.route(h, body, cls, None, None)
        if decision.suppress:
            buckets.append(decision.event_bucket)
            keys2.append(None); keys3.append(None)
            classes.append(None); suppressed.append(True)
            continue
        addendum_type = decision.company_type if row.n_hop0 == 1 else None
        k2, b2 = v2_composition(h, body)
        k3, b3 = v3_composition(h, body, decision, addendum_type)
        buckets.append(decision.event_bucket)
        keys2.append(k2); keys3.append(k3)
        classes.append(classify_diff(k2, b2, k3, b3, decision))
        suppressed.append(False)
    d["event_bucket"] = buckets
    d["key_v2"] = keys2
    d["key_v3"] = keys3
    d["diff_class"] = classes
    d["router_suppressed"] = suppressed
    d["label_ok"] = np.isfinite(d["capturable_ar"].astype(float))
    return d


# ── Phase 2: seed-42 stratified sample ────────────────────────────────────

def stratified_sample(affected, cap: int):
    """Proportional per difference-class with a min-30 floor (where the class
    has that many), seed 42, on rows pre-sorted by (impact_id, symbol)."""
    import numpy as np
    rng = np.random.default_rng(SEED)
    groups = {c: g.index.to_list()
              for c, g in affected.groupby("diff_class", sort=True)}
    total = sum(len(v) for v in groups.values())
    if total <= cap:
        return affected.index.to_list(), {c: len(v) for c, v in groups.items()}

    base = {c: min(MIN_PER_CLASS, len(v)) for c, v in groups.items()}
    spare = cap - sum(base.values())
    if spare < 0:  # pathological tiny cap: scale the floors themselves
        quota = {c: max(1, int(cap * len(v) / total)) for c, v in groups.items()}
    else:
        rem = {c: len(v) - base[c] for c, v in groups.items()}
        rem_total = sum(rem.values())
        exact = {c: (spare * rem[c] / rem_total if rem_total else 0.0)
                 for c in groups}
        quota = {c: base[c] + int(exact[c]) for c in groups}
        leftover = cap - sum(quota.values())
        # largest-remainder rounding, ties broken by class name (deterministic)
        order = sorted(groups, key=lambda c: (-(exact[c] - int(exact[c])), c))
        for c in order:
            if leftover <= 0:
                break
            if quota[c] < len(groups[c]):
                quota[c] += 1
                leftover -= 1
    picked: List[int] = []
    for c in sorted(groups):
        ids = groups[c]
        k = min(quota.get(c, 0), len(ids))
        if k >= len(ids):
            picked.extend(ids)
        else:
            picked.extend(rng.choice(ids, size=k, replace=False).tolist())
    picked = sorted(picked)
    by_class = {c: int(min(quota.get(c, 0), len(v))) for c, v in groups.items()}
    return picked, by_class


# ── Phase 3: replay ───────────────────────────────────────────────────────

def _import_stage3():
    """Deferred so --diff-only never needs the LLM/stage-3 import surface."""
    from bharat_terminal.analysis.stages import stage3_impact as s3
    return s3


def _to_ist(ts):
    from bharat_terminal.analysis.market_hours import IST
    import pandas as pd
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(IST).to_pydatetime()


def assemble_pair(s3, row, body_rec: dict, ctx: Optional[dict]):
    """Both arms' prompts through the REAL production assembly path.
    Returns (prompt_v2, prompt_v3, decision, fullbody_class, key2, key3)."""
    from bharat_terminal.types import NewsItem

    headline = body_rec.get("headline") or row.headline or ""
    full_body = body_rec.get("body") or ""
    ni = NewsItem(
        id=str(row.impact_id), source=body_rec.get("source") or row.source,
        timestamp_utc=_to_ist(row.created_at).astimezone(timezone.utc),
        headline=headline, url=body_rec.get("url") or "replay://",
        ingest_latency_ms=0.0, category=row.category, full_body=full_body,
    )
    cls = (ctx or {}).get("classification")
    mstate = (ctx or {}).get("market_state")
    decision = router_v2.route(headline, full_body, cls, mstate,
                               _to_ist(row.created_at))
    contexts = {row.symbol: ctx} if ctx else {}
    decisions = {row.symbol: decision}
    with _rubrics_flag(False):
        p2 = s3.build_impact_prompt(ni, [row.symbol], contexts, {},
                                    market_sector_backdrop="",
                                    route_decisions=decisions)
    with _rubrics_flag(True):
        p3 = s3.build_impact_prompt(ni, [row.symbol], contexts, {},
                                    market_sector_backdrop="",
                                    route_decisions=decisions)
    # Recompute the difference-class on the FULL body (audit trail: the cached
    # 8K diff may shift once the sniff sees the whole filing).
    k2, b2 = v2_composition(headline, full_body)
    addendum_type = decision.company_type  # single-symbol replay contract
    k3, b3 = v3_composition(headline, full_body, decision, addendum_type)
    fb_class = classify_diff(k2, b2, k3, b3, decision)
    return p2, p3, decision, fb_class, k2, k3


def _parse_impact(resp) -> Optional[dict]:
    """First impact from a forced score_company_impacts call (single-symbol
    contract: whatever identifier the model echoed, the impact is ours)."""
    tool = resp.first_tool_use("score_company_impacts")
    if not tool:
        return None
    impacts = tool.get("impacts") or []
    if not impacts or not isinstance(impacts[0], dict):
        return None
    item = impacts[0]
    sent = item.get("sentiment")
    mag = item.get("magnitude")
    if sent not in ("positive", "negative", "neutral"):
        return None
    if not isinstance(mag, (int, float)):
        return None
    return {
        "sentiment": sent,
        "magnitude": int(max(1, min(5, int(mag)))),
        "time_horizon": item.get("time_horizon") or "",
        "explanation": (item.get("explanation") or "")[:300],
    }


async def replay_llm(s3, jobs: List[dict], concurrency: int) -> Tuple[dict, float, int]:
    """jobs: [{key, prompt}] -> {key: parsed|None}, actual cost, parse fails.
    Retry-once on parse failure or transport error; temperature 0."""
    from bharat_terminal import llm
    from bharat_terminal.analysis.llm_logger import compute_cost

    model = s3.LLM_MODEL
    sem = asyncio.Semaphore(concurrency)
    results: Dict[Any, Optional[dict]] = {}
    cost = {"usd": 0.0}
    done = {"n": 0}

    async def one(job):
        async with sem:
            parsed = None
            for attempt in (1, 2):   # retry-once
                try:
                    resp = await llm.acomplete(
                        model=model, max_tokens=2048,
                        tools=[s3.IMPACT_TOOL],
                        tool_choice={"type": "tool",
                                     "name": "score_company_impacts"},
                        temperature=0.0,
                        messages=[{"role": "user", "content": job["prompt"]}],
                    )
                except Exception as e:
                    logger.warning("replay call failed (attempt %d) %s: %s",
                                   attempt, job["key"], str(e)[:200])
                    continue
                cost["usd"] += compute_cost(
                    model, resp.usage.input_tokens,
                    resp.usage.output_tokens + resp.usage.thinking_tokens)
                parsed = _parse_impact(resp)
                if parsed is not None:
                    break
                logger.warning("parse failure (attempt %d) %s", attempt, job["key"])
            results[job["key"]] = parsed
            done["n"] += 1
            if done["n"] % 50 == 0:
                logger.info("replay progress: %d/%d calls, cost so far $%.2f",
                            done["n"], len(jobs), cost["usd"])

    await asyncio.gather(*(one(j) for j in jobs))
    fails = sum(1 for v in results.values() if v is None)
    return results, cost["usd"], fails


async def run_replay(sampled, concurrency: int, force: bool) -> dict:
    """Phase 3 driver: bodies + KB contexts + prompt pairs + LLM calls."""
    s3 = _import_stage3()

    bodies = await fetch_full_bodies(sampled["impact_id"].tolist())
    missing_body = [i for i in sampled["impact_id"] if i not in bodies]

    # KB context ONCE per symbol, exactly stage-3's fetch, shared by both arms.
    contexts: Dict[str, Optional[dict]] = {}

    async def fetch_ctx(sym: str):
        for _ in range(KB_FETCH_ATTEMPTS):
            ctx = await s3.fetch_company_context(sym)
            if ctx is not None:
                contexts[sym] = ctx
                return
            await asyncio.sleep(0.5)
        contexts[sym] = None

    ksem = asyncio.Semaphore(10)

    async def bounded_fetch(sym):
        async with ksem:
            await fetch_ctx(sym)

    await asyncio.gather(*(bounded_fetch(s)
                           for s in sorted(sampled["symbol"].unique())))
    kb_miss = sorted(s for s, c in contexts.items() if c is None)

    pairs, skipped = [], {"no_body": 0, "kb_miss": 0, "prompts_identical": 0}
    for row in sampled.itertuples(index=False):
        rec = bodies.get(row.impact_id)
        if rec is None:
            skipped["no_body"] += 1
            continue
        ctx = contexts.get(row.symbol)
        if ctx is None:
            # Without the KB payload the v3 arm loses its classification axis —
            # the pair would not measure Tier B. Dropped, counted loudly.
            skipped["kb_miss"] += 1
            continue
        p2, p3, decision, fb_class, k2, k3 = assemble_pair(s3, row, rec, ctx)
        if p2 == p3:
            skipped["prompts_identical"] += 1
            continue
        pairs.append({
            "impact_id": row.impact_id, "symbol": row.symbol,
            "diff_class_cached": row.diff_class, "diff_class_fullbody": fb_class,
            "key_v2": k2, "key_v3": k3, "event_bucket": decision.event_bucket,
            "para_a": decision.para_a, "para_a_leaf": decision.para_a_leaf,
            "prompt_v2": p2, "prompt_v3": p3,
        })

    # Cost estimate BEFORE any call (chars/4 ≈ tokens; project's own pricing).
    from bharat_terminal.analysis.llm_logger import compute_cost
    est = sum(compute_cost(s3.LLM_MODEL, len(p[f"prompt_{a}"]) // 4,
                           EST_OUTPUT_TOKENS)
              for p in pairs for a in ("v2", "v3"))
    print(f"REPLAY PLAN: {len(pairs)} pairs = {2 * len(pairs)} calls, "
          f"model={s3.LLM_MODEL}, projected cost ≈ ${est:.2f} "
          f"(chars/4 tokens, {EST_OUTPUT_TOKENS} out)")
    if est > COST_ABORT_USD and not force:
        raise SystemExit(
            f"ABORT: projected cost ${est:.2f} > ${COST_ABORT_USD:.0f} cap. "
            f"Re-run with --force to override, or lower --max-n.")

    jobs = [{"key": (p["impact_id"], p["symbol"], arm),
             "prompt": p[f"prompt_{arm}"]}
            for p in pairs for arm in ("v2", "v3")]
    results, actual_cost, parse_fails = await replay_llm(s3, jobs, concurrency)

    rows = []
    for p in pairs:
        out = {k: v for k, v in p.items()
               if k not in ("prompt_v2", "prompt_v3")}
        complete = True
        for arm in ("v2", "v3"):
            r = results.get((p["impact_id"], p["symbol"], arm))
            if r is None:
                complete = False
                out[f"{arm}_ok"] = False
                continue
            mag_floored, floored = router_v2.apply_para_a_floor(
                r["magnitude"], p["para_a"])
            out[f"{arm}_ok"] = True
            out[f"{arm}_sentiment"] = r["sentiment"]
            out[f"{arm}_magnitude_raw"] = r["magnitude"]
            out[f"{arm}_magnitude"] = mag_floored
            out[f"{arm}_para_a_floored"] = floored
            out[f"{arm}_horizon"] = r["time_horizon"]
            out[f"{arm}_explanation"] = r["explanation"]
        out["pair_complete"] = complete
        rows.append(out)

    return {
        "model": s3.LLM_MODEL, "temperature": 0.0,
        "concurrency": concurrency,
        "n_sampled": int(len(sampled)), "n_pairs_called": len(pairs),
        "skipped": skipped, "missing_body_ids": missing_body[:20],
        "kb_miss_symbols": kb_miss[:20], "n_kb_miss_symbols": len(kb_miss),
        "est_cost_usd": round(est, 2), "actual_cost_usd": round(actual_cost, 2),
        "parse_failures_after_retry": parse_fails,
        "rows": rows,
    }


# ── Phase 4: scoring ──────────────────────────────────────────────────────

def _band(mag: int) -> str:
    return "high" if mag >= 4 else ("medium" if mag == 3 else "low")


def _arm_metrics(rdf, arm: str) -> dict:
    """hard_wrong (non-neutral denominator) + direction hit vs
    sign(capturable_ar), per the harness spec."""
    import numpy as np
    sent = rdf[f"{arm}_sentiment"]
    ar = rdf["capturable_ar"].astype(float)
    nn = sent.isin(["positive", "negative"])
    hw = ((sent == "positive") & (ar <= -HARD_WRONG_PCT)) | \
         ((sent == "negative") & (ar >= HARD_WRONG_PCT))
    hit = ((sent == "positive") & (ar > 0)) | ((sent == "negative") & (ar < 0))
    return {
        "n": int(len(rdf)),
        "n_nonneutral": int(nn.sum()),
        "neutral_share": round(float(1 - nn.mean()), 4) if len(rdf) else None,
        "hard_wrong_rate": round(float(hw[nn].mean()), 4) if nn.any() else None,
        "hard_wrong_n": int(hw[nn].sum()),
        "direction_hit_rate": round(float(hit[nn].mean()), 4) if nn.any() else None,
        "mean_magnitude": round(float(rdf[f"{arm}_magnitude"].mean()), 3),
    }


def _monotonicity(rdf, arm: str) -> dict:
    out = {}
    bands = rdf[f"{arm}_magnitude"].map(_band)
    for b in ("low", "medium", "high"):
        g = rdf[bands == b]
        out[b] = {"n": int(len(g)),
                  "p_material_capturable":
                      round(float(g["material_capturable"].mean()), 4)
                      if len(g) else None}
    return out


def _gate_scores(rdf, frame, arm: str, artifact):
    """v4.1 frozen-gate probability per row, features built from the arm's
    outputs via mc.featurize conventions (tax22 taxonomy, cached 8K body —
    the text representation the artifact was trained on)."""
    import numpy as np
    df = frame.loc[rdf["frame_idx"]].copy()
    df["magnitude"] = rdf[f"{arm}_magnitude"].values
    df["sentiment"] = rdf[f"{arm}_sentiment"].values
    df["horizon"] = rdf[f"{arm}_horizon"].fillna("").values
    orig = mc.event_type
    try:
        mc.event_type = ml.event_type_v2
        X, _y_cc, dff = mc.featurize(df)
    finally:
        mc.event_type = orig
    X = X.reindex(columns=artifact["feature_columns"], fill_value=0)
    text = (dff["headline"].fillna("") + " " + dff["body"].fillna(""))
    E = artifact["svd"].transform(artifact["vec"].transform(text))
    p = (artifact["lr"].predict_proba(X.values)[:, 1]
         + artifact["hgb"].predict_proba(np.hstack([X.values, E]))[:, 1]) / 2
    return p, X, dff


def _lr_refit_auc(X, y, created_at) -> dict:
    """Quick per-arm LR refit: 70/30 time split when n allows, else 5-fold."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_score
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    y = np.asarray(y)
    model = make_pipeline(StandardScaler(), LogisticRegression(
        C=0.1, max_iter=4000, random_state=SEED))
    cutoff = created_at.quantile(0.7)
    tr = (created_at <= cutoff).values
    te = ~tr
    time_split_ok = (len(y) >= 400 and te.sum() >= 60
                     and len(set(y[tr])) == 2 and len(set(y[te])) == 2)
    if time_split_ok:
        model.fit(X.values[tr], y[tr])
        auc = roc_auc_score(y[te], model.predict_proba(X.values[te])[:, 1])
        return {"method": "time_split_70_30", "n_test": int(te.sum()),
                "auc": round(float(auc), 4)}
    if len(set(y)) < 2 or min((y == 1).sum(), (y == 0).sum()) < 5:
        return {"method": "insufficient_labels", "auc": None,
                "n_pos": int((y == 1).sum())}
    cv = StratifiedKFold(5, shuffle=True, random_state=SEED)
    aucs = cross_val_score(model, X.values, y, scoring="roc_auc", cv=cv)
    return {"method": "cv_5fold_stratified", "auc": round(float(aucs.mean()), 4),
            "auc_folds": [round(float(a), 4) for a in aucs]}


def _paired_bootstrap(rdf) -> dict:
    """Seed-42 bootstrap (10,000 resamples over events) of the v3−v2 deltas
    on hard_wrong and direction-hit."""
    import numpy as np
    rng = np.random.default_rng(SEED)
    n = len(rdf)
    ar = rdf["capturable_ar"].astype(float).values

    def arrays(arm):
        sent = rdf[f"{arm}_sentiment"].values
        nn = np.isin(sent, ("positive", "negative"))
        hw = ((sent == "positive") & (ar <= -HARD_WRONG_PCT)) | \
             ((sent == "negative") & (ar >= HARD_WRONG_PCT))
        hit = ((sent == "positive") & (ar > 0)) | ((sent == "negative") & (ar < 0))
        return nn, hw, hit

    nn2, hw2, hit2 = arrays("v2")
    nn3, hw3, hit3 = arrays("v3")

    def rates(idx):
        d2, d3 = nn2[idx], nn3[idx]
        hw_d = ((hw3[idx][d3].mean() if d3.any() else np.nan)
                - (hw2[idx][d2].mean() if d2.any() else np.nan))
        hit_d = ((hit3[idx][d3].mean() if d3.any() else np.nan)
                 - (hit2[idx][d2].mean() if d2.any() else np.nan))
        return hw_d, hit_d

    point_hw, point_hit = rates(np.arange(n))
    hw_s, hit_s = [], []
    for _ in range(BOOTSTRAP_N):
        idx = rng.integers(0, n, n)
        a, b = rates(idx)
        hw_s.append(a); hit_s.append(b)
    hw_s = np.array(hw_s, dtype=float)
    hit_s = np.array(hit_s, dtype=float)

    def ci(a):
        a = a[np.isfinite(a)]
        return [round(float(np.percentile(a, 2.5)), 4),
                round(float(np.percentile(a, 97.5)), 4)]

    return {
        "n_events": n, "n_resamples": BOOTSTRAP_N, "seed": SEED,
        "delta_hard_wrong": round(float(point_hw), 4),
        "delta_hard_wrong_ci95": ci(hw_s),
        "delta_direction_hit": round(float(point_hit), 4),
        "delta_direction_hit_ci95": ci(hit_s),
    }


def score(replay: dict, frame) -> dict:
    """All step-4 metrics on completed pairs (identical rows both arms)."""
    import numpy as np
    import pandas as pd

    rows = [r for r in replay["rows"] if r.get("pair_complete")]
    if not rows:
        return {"error": "no completed pairs to score"}
    rdf = pd.DataFrame(rows)

    fidx = frame.reset_index().set_index(["impact_id", "symbol"])
    key = list(zip(rdf["impact_id"], rdf["symbol"]))
    sel = fidx.loc[key]
    rdf["frame_idx"] = sel["index"].values
    for c in ("capturable_ar", "material_capturable", "created_at"):
        rdf[c] = sel[c].values

    out: Dict[str, Any] = {"n_pairs_scored": int(len(rdf))}
    out["v2"] = _arm_metrics(rdf, "v2")
    out["v3"] = _arm_metrics(rdf, "v3")
    out["paired"] = _paired_bootstrap(rdf)

    fr_mask = rdf["diff_class_fullbody"].isin(FUND_RAISE_CLASSES)
    wrap_mask = rdf["diff_class_fullbody"] == "wrapper_content_sniff"
    out["magnitude_band_monotonicity"] = {
        "overall": {a: _monotonicity(rdf, a) for a in ("v2", "v3")},
        "fund_raise_class": {a: _monotonicity(rdf[fr_mask], a)
                             for a in ("v2", "v3")},
        "wrapper_reroute_class": {a: _monotonicity(rdf[wrap_mask], a)
                                  for a in ("v2", "v3")},
    }

    # Protect check: affected RESULTS rows classified negative — direction
    # hit must not degrade (reported, both arms).
    res_mask = (rdf["key_v2"] == "financial_results") | \
               (rdf["key_v3"] == "financial_results")
    protect = {}
    for a in ("v2", "v3"):
        g = rdf[res_mask & (rdf[f"{a}_sentiment"] == "negative")]
        hit = (g["capturable_ar"].astype(float) < 0)
        protect[a] = {"n": int(len(g)),
                      "direction_hit_rate":
                          round(float(hit.mean()), 4) if len(g) else None}
    out["protect_results_negative"] = protect

    # Frozen v4.1 gate + quick LR refit, per arm, same rows both arms.
    try:
        import joblib
        artifact = joblib.load(GATE_ARTIFACT)
        y = rdf["material_capturable"].astype(int).values
        base = float(np.mean(y))
        gate = {"artifact": GATE_ARTIFACT,
                "label": "material_capturable", "base_rate": round(base, 4)}
        for a in ("v2", "v3"):
            p, X, dff = _gate_scores(rdf, frame, a, artifact)
            if len(set(y)) == 2:
                gate[f"frozen_{a}"] = ml.evaluate(p, y, base)
            else:
                gate[f"frozen_{a}"] = {"auc": None, "note": "single-class labels"}
            gate[f"lr_refit_{a}"] = _lr_refit_auc(
                X, y, pd.Series(rdf["created_at"].values))
        out["gate"] = gate
    except Exception as e:
        out["gate"] = {"error": f"gate scoring failed: {e}"}
    return out


# ── Reporting / persistence ───────────────────────────────────────────────

def population_report(d) -> dict:
    scored = d[~d["router_suppressed"]]
    affected = scored[scored["diff_class"].notna()]
    by_class = {c: int(n) for c, n
                in affected["diff_class"].value_counts().items()}
    for c in DIFF_CLASSES:
        by_class.setdefault(c, 0)
    return {
        "hop0_rows": int(len(d)),
        "router_suppressed": int(d["router_suppressed"].sum()),
        "multi_hop0_rows": int((d["n_hop0"] > 1).sum()),
        "affected_total": int(len(affected)),
        "affected_share": round(float(len(affected) / max(len(scored), 1)), 4),
        "affected_by_class": by_class,
        "affected_label_missing": int((~affected["label_ok"]).sum()),
        "cache_body_chars": ml.BODY_CHARS,
        "notes": [
            "diff computed on the CACHED 8K body; production sniff/lender "
            "windows are 40K — sniff-driven classes can undercount here",
            "market_state omitted from the diff routing (state flags do not "
            "enter Tier-B rubric selection)",
            "approvals/auditor buckets do not currently alter scenario_v3 "
            "selection — zero counts there are structural, not measured absence",
        ],
    }


def _ledger(rec: dict) -> None:
    ml._ledger_write({"exp": "RB", "seed": SEED,
                      "run_at": datetime.now(timezone.utc).isoformat(), **rec})


def _out_path() -> str:
    return f"{LAB_DIR}/replay_tierb_{datetime.now(timezone.utc).date().isoformat()}.json"


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--diff-only", action="store_true",
                    help="Compute + report the affected population; NO LLM calls.")
    ap.add_argument("--run", action="store_true",
                    help="Execute the paired replay (LLM spend).")
    ap.add_argument("--max-n", type=int, default=DEFAULT_CAP)
    ap.add_argument("--force", action="store_true",
                    help="Override the $%.0f projected-cost abort." % COST_ABORT_USD)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    args = ap.parse_args()
    if not (args.diff_only or args.run):
        ap.error("choose --diff-only (first run) or --run")

    d = load_frame(args.days)
    logger.info("hop-0 frame: %d rows, %s -> %s", len(d),
                d["created_at"].min(), d["created_at"].max())

    cls_map = asyncio.run(fetch_classifications(d["symbol"].unique().tolist()))
    logger.info("classifications fetched for %d symbols (one query)", len(cls_map))
    d = compute_population(d, cls_map)
    pop = population_report(d)
    print(json.dumps({"population": pop}, indent=2, default=str))

    # Sample plan (also reported in --diff-only so the --run cost is knowable).
    eligible = d[(~d["router_suppressed"]) & d["diff_class"].notna()
                 & d["label_ok"] & (d["n_hop0"] == 1)]
    excluded_multi = int((d["diff_class"].notna() & (d["n_hop0"] > 1)
                          & ~d["router_suppressed"]).sum())
    picked, sample_by_class = stratified_sample(eligible, args.max_n)
    sample_plan = {
        "eligible_affected": int(len(eligible)),
        "excluded_multi_hop0": excluded_multi,
        "excluded_label_missing": int(pop["affected_label_missing"]),
        "max_n": args.max_n, "selected": len(picked),
        "selected_by_class": sample_by_class,
    }
    print(json.dumps({"sample": sample_plan}, indent=2, default=str))

    result: Dict[str, Any] = {
        "exp": "RB", "seed": SEED, "days": args.days,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "mode": "diff_only" if not args.run else "run",
        "population": pop, "sample": sample_plan,
    }
    _ledger({"variant": "diff_population", **pop["affected_by_class"],
             "affected_total": pop["affected_total"],
             "hop0_rows": pop["hop0_rows"]})

    if args.run and not picked:
        print("nothing to replay: no eligible affected rows")
    elif args.run:
        sampled = eligible.loc[picked]
        replay = asyncio.run(run_replay(sampled, args.concurrency, args.force))
        scoring = score(replay, d)
        result["replay"] = replay
        result["scoring"] = scoring
        for arm in ("v2", "v3"):
            if isinstance(scoring.get(arm), dict):
                _ledger({"variant": f"arm_{arm}", **scoring[arm],
                         "gate": (scoring.get("gate") or {}).get(f"frozen_{arm}"),
                         "lr_refit": (scoring.get("gate") or {}).get(f"lr_refit_{arm}")})
        if isinstance(scoring.get("paired"), dict):
            _ledger({"variant": "paired_delta", **scoring["paired"],
                     "protect": scoring.get("protect_results_negative")})
        print(json.dumps({"scoring": {k: v for k, v in scoring.items()
                                      if k != "rows"}}, indent=2, default=str))

    os.makedirs(LAB_DIR, exist_ok=True)
    with open(_out_path(), "w") as f:
        json.dump(result, f, indent=2, default=str)
    logger.info("wrote %s", _out_path())


if __name__ == "__main__":
    main()

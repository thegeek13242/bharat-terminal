"""
Dossier endpoints — agent-produced CFA-level analysis browser + 3D impact graph data.

  GET /dossier/list                              — paginated index of all dossiers
  GET /dossier/{symbol}                          — full dossier JSONB + sensitivities + linkages
  GET /dossier/exposed?trigger=BRENT             — which companies are exposed to a trigger
  GET /dossier/impact-graph?trigger=BRENT         — 2-hop graph for 3D viz: trigger → 1st → 2nd
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dossier", tags=["dossier"])


@router.get("/list")
async def list_dossiers(
    min_quality: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
):
    """All dossiers with quality / freshness metadata."""
    factory = get_session_factory()
    async with factory() as s:
        total = (await s.execute(text(
            "SELECT COUNT(*) FROM company_dossiers WHERE data_quality >= :q"
        ), {"q": min_quality})).scalar_one()
        rows = (await s.execute(text("""
            SELECT d.symbol, c.company_name, d.version, d.source_quarter,
                   d.refresh_kind, d.data_quality, d.model_used,
                   d.tokens_used, d.generated_at,
                   (SELECT COUNT(*) FROM dossier_sensitivities WHERE symbol = d.symbol) AS sens_count,
                   (SELECT COUNT(*) FROM dossier_linkages WHERE source_symbol = d.symbol) AS link_count
            FROM company_dossiers d
            LEFT JOIN companies c ON c.symbol = d.symbol
            WHERE d.data_quality >= :q
            ORDER BY d.generated_at DESC
            LIMIT :limit OFFSET :offset
        """), {"q": min_quality, "limit": limit, "offset": offset})).mappings().all()
    return {"total": total, "limit": limit, "offset": offset, "items": [dict(r) for r in rows]}


@router.get("/{symbol}")
async def get_dossier(symbol: str):
    """Full dossier for a single symbol: JSONB sections + flattened sensitivity + linkage rows."""
    factory = get_session_factory()
    sym = symbol.upper()
    async with factory() as s:
        head = (await s.execute(text("""
            SELECT d.symbol, c.company_name, d.version, d.source_quarter,
                   d.refresh_kind, d.data_quality, d.model_used, d.tokens_used,
                   d.generated_at,
                   d.business, d.financials, d.sensitivities AS sensitivities_full,
                   d.linkages AS linkages_full,
                   d.risks, d.catalysts, d.mgmt_commentary, d.citations
            FROM company_dossiers d
            LEFT JOIN companies c ON c.symbol = d.symbol
            WHERE d.symbol = :symbol
        """), {"symbol": sym})).mappings().first()
        if not head:
            raise HTTPException(status_code=404, detail=f"No dossier for {symbol}")

        sens = (await s.execute(text("""
            SELECT trigger_id, beta, channel, lag_days, confidence,
                   rationale, evidence_url
            FROM dossier_sensitivities WHERE symbol = :symbol
            ORDER BY ABS(beta) DESC
        """), {"symbol": sym})).mappings().all()

        links = (await s.execute(text("""
            SELECT target_symbol, kind, strength, direction,
                   propagation_lag_days, rationale, evidence_url
            FROM dossier_linkages WHERE source_symbol = :symbol
            ORDER BY strength DESC
        """), {"symbol": sym})).mappings().all()

    result = dict(head)
    result["sensitivities"] = [dict(r) for r in sens]
    result["linkages"] = [dict(r) for r in links]
    return result


@router.get("/exposed/")
async def get_exposed(
    trigger: str = Query(..., description="Trigger ID, e.g. BRENT"),
    min_abs_beta: float = Query(0.05, ge=0.0),
    channel: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    """Companies most exposed to a given trigger — the 1st-degree fanout."""
    factory = get_session_factory()
    async with factory() as s:
        where = ["s.trigger_id = :trigger", "ABS(s.beta) >= :min_abs_beta"]
        params = {"trigger": trigger.upper(), "min_abs_beta": min_abs_beta, "limit": limit}
        if channel:
            where.append("s.channel = :channel")
            params["channel"] = channel
        rows = (await s.execute(text(f"""
            SELECT s.symbol, c.company_name, s.beta, s.channel, s.lag_days,
                   s.confidence, s.rationale, s.evidence_url
            FROM dossier_sensitivities s
            LEFT JOIN companies c ON c.symbol = s.symbol
            WHERE {' AND '.join(where)}
            ORDER BY ABS(s.beta) DESC
            LIMIT :limit
        """), params)).mappings().all()
    return {"trigger": trigger.upper(), "count": len(rows), "items": [dict(r) for r in rows]}


@router.get("/impact-graph/")
async def get_impact_graph(
    trigger: str = Query(..., description="Trigger ID, e.g. BRENT"),
    max_hops: int = Query(2, ge=1, le=3),
    min_abs_beta: float = Query(0.05, ge=0.0),
    limit_first_degree: int = Query(20, le=50),
    limit_per_node: int = Query(5, le=20, description="Max outbound edges per 1st-degree node"),
):
    """
    Build a 2-3 hop graph centered on a trigger, suitable for a 3D force-graph render.

    Returns:
      nodes: [{id, type, label, hop, beta?, channel?, lag_days?, ...}]
      links: [{source, target, kind, strength|beta, channel?, lag_days?}]

    Hop semantics:
      0 = trigger itself (one root node, type='trigger')
      1 = companies directly sensitive to the trigger (one per company × channel pair,
          but we collapse to one node per company with their strongest channel)
      2 = companies linked to a 1st-degree company (competitor/sector_peer/customer/supplier)
    """
    factory = get_session_factory()
    t = trigger.upper()
    async with factory() as s:
        # 1st-degree: most exposed companies
        first = (await s.execute(text("""
            SELECT s.symbol, c.company_name,
                   s.beta, s.channel, s.lag_days, s.confidence
            FROM dossier_sensitivities s
            LEFT JOIN companies c ON c.symbol = s.symbol
            WHERE s.trigger_id = :trigger
              AND ABS(s.beta) >= :min_abs_beta
            ORDER BY ABS(s.beta) DESC
            LIMIT :limit
        """), {"trigger": t, "min_abs_beta": min_abs_beta, "limit": limit_first_degree})).mappings().all()
        first = [dict(r) for r in first]

        # Collapse per-symbol — pick the strongest channel for graph node attributes
        first_by_symbol: dict[str, dict] = {}
        for r in first:
            sym = r["symbol"]
            if sym not in first_by_symbol or abs(r["beta"]) > abs(first_by_symbol[sym]["beta"]):
                first_by_symbol[sym] = r

        first_symbols = list(first_by_symbol.keys())

        # 2nd-degree: linkages outbound from any 1st-degree node, top N per source
        second_raw = []
        if max_hops >= 2 and first_symbols:
            second_raw = (await s.execute(text("""
                SELECT DISTINCT ON (l.source_symbol, l.target_symbol, l.kind)
                       l.source_symbol, l.target_symbol, l.kind,
                       l.strength, l.direction, l.propagation_lag_days
                FROM dossier_linkages l
                WHERE l.source_symbol = ANY(:symbols)
                  AND l.target_symbol NOT IN :first_symbols
                ORDER BY l.source_symbol, l.target_symbol, l.kind, l.strength DESC
            """), {
                "symbols": first_symbols,
                "first_symbols": tuple(first_symbols),
            })).mappings().all()
            second_raw = [dict(r) for r in second_raw]

        # Cap per source to avoid hairballs
        per_source: dict[str, int] = {}
        second_filtered = []
        for r in sorted(second_raw, key=lambda x: -float(x["strength"])):
            if per_source.get(r["source_symbol"], 0) >= limit_per_node:
                continue
            per_source[r["source_symbol"]] = per_source.get(r["source_symbol"], 0) + 1
            second_filtered.append(r)

        # Resolve target names where possible
        target_syms = list({r["target_symbol"] for r in second_filtered})
        target_names = {}
        if target_syms:
            tn = (await s.execute(text("""
                SELECT symbol, company_name FROM companies
                WHERE symbol = ANY(:syms)
            """), {"syms": target_syms})).mappings().all()
            target_names = {r["symbol"]: r["company_name"] for r in tn}

    # ── Assemble graph payload ──────────────────────────────────────────
    nodes = [
        {
            "id": f"trigger:{t}",
            "type": "trigger",
            "label": t,
            "hop": 0,
        }
    ]

    for sym, r in first_by_symbol.items():
        nodes.append({
            "id": sym,
            "type": "company",
            "label": sym,
            "company_name": r["company_name"],
            "hop": 1,
            "beta": float(r["beta"]),
            "channel": r["channel"],
            "lag_days": r["lag_days"],
            "confidence": r["confidence"],
        })

    second_targets = {r["target_symbol"] for r in second_filtered}
    for sym in second_targets:
        nodes.append({
            "id": sym,
            "type": "company",
            "label": sym,
            "company_name": target_names.get(sym),
            "hop": 2,
        })

    links = []
    # trigger → 1st degree
    for sym, r in first_by_symbol.items():
        links.append({
            "source": f"trigger:{t}",
            "target": sym,
            "kind": "sensitivity",
            "channel": r["channel"],
            "beta": float(r["beta"]),
            "lag_days": r["lag_days"],
            "value": abs(float(r["beta"])),  # for thickness
        })
    # 1st → 2nd
    for r in second_filtered:
        links.append({
            "source": r["source_symbol"],
            "target": r["target_symbol"],
            "kind": r["kind"],
            "strength": float(r["strength"]),
            "direction": r["direction"],
            "lag_days": r["propagation_lag_days"],
            "value": float(r["strength"]) * 0.7,
        })

    return {
        "trigger": t,
        "nodes": nodes,
        "links": links,
        "stats": {
            "n_first_degree": len(first_by_symbol),
            "n_second_degree": len(second_targets),
            "n_links": len(links),
        },
    }


@router.get("/triggers/")
async def list_triggers_with_coverage():
    """All triggers + how many dossier sensitivities reference each."""
    factory = get_session_factory()
    async with factory() as s:
        rows = (await s.execute(text("""
            SELECT t.id, t.label, t.kind, t.unit, t.data_source,
                   (SELECT COUNT(*) FROM dossier_sensitivities WHERE trigger_id = t.id) AS coverage_count,
                   (SELECT MAX(ABS(beta)) FROM dossier_sensitivities WHERE trigger_id = t.id) AS max_abs_beta
            FROM triggers t
            ORDER BY coverage_count DESC, t.id
        """))).mappings().all()
    return {"count": len(rows), "items": [dict(r) for r in rows]}

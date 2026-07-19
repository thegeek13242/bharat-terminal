"""Concall transcript endpoints — the COMPLETE transcript, user-visible.

Transcripts are public exchange filings (no licensing constraint — unlike
live LTP). `full_text` is stored unclipped by kb/sync/transcript_sync.py and
is served whole here: never truncate it in this layer.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from bharat_terminal.api.auth import get_current_user
from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/transcripts", tags=["transcripts"])

_LIST_COLS = """
    ct.id, ct.symbol, c.company_name, ct.headline, ct.announced_at,
    ct.pdf_url, ct.page_count, ct.char_count, ct.parse_status
"""


def _list_row(r) -> dict:
    return {
        "id": r.id,
        "symbol": r.symbol,
        "company_name": r.company_name,
        "headline": r.headline,
        "announced_at": r.announced_at.isoformat() if r.announced_at else None,
        "pdf_url": r.pdf_url,
        "page_count": r.page_count,
        "char_count": r.char_count,
        "parse_status": r.parse_status,
    }


@router.get("/recent")
async def recent_transcripts(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(get_current_user),
):
    """Recently filed transcripts across the universe (metadata only)."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = (await session.execute(text(f"""
            SELECT {_LIST_COLS}
            FROM concall_transcripts ct
            LEFT JOIN companies c ON c.symbol = ct.symbol
            WHERE ct.announced_at >= now() - make_interval(days => :days)
              AND ct.parse_status = 'ok'
            ORDER BY ct.announced_at DESC
            LIMIT :limit
        """), {"days": days, "limit": limit})).all()
    return {"items": [_list_row(r) for r in rows], "count": len(rows)}


@router.get("/id/{transcript_id}")
async def get_transcript(
    transcript_id: int,
    user: dict = Depends(get_current_user),
):
    """One transcript WITH its complete text. Served whole — no truncation."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        r = (await session.execute(text(f"""
            SELECT {_LIST_COLS}, ct.full_text, ct.structure, ct.structure_status
            FROM concall_transcripts ct
            LEFT JOIN companies c ON c.symbol = ct.symbol
            WHERE ct.id = :tid
        """), {"tid": transcript_id})).first()
    if not r:
        raise HTTPException(status_code=404, detail="transcript_not_found")
    out = _list_row(r)
    out["full_text"] = r.full_text
    # Annotation layer (speakers/segments/highlights/pages) — offsets into
    # full_text, verbatim-anchored at build time. Null when structuring is
    # pending/failed; the client falls back to the raw reader.
    out["structure"] = r.structure
    out["structure_status"] = r.structure_status
    async with session_factory() as session:
        grows = (await session.execute(text("""
            SELECT g.id, g.metric, g.period, g.value_low, g.value_high,
                   g.unit, g.quote_start, g.quote_end, g.change_verdict,
                   pg.period AS prior_period, pg.value_low AS prior_low,
                   pg.value_high AS prior_high, pg.unit AS prior_unit,
                   pg.transcript_id AS prior_transcript_id,
                   pg.quote_start AS prior_quote_start,
                   pg.quote_end AS prior_quote_end
            FROM transcript_guidance g
            LEFT JOIN transcript_guidance pg ON pg.id = g.prior_guidance_id
            WHERE g.transcript_id = :tid
            ORDER BY g.quote_start
        """), {"tid": transcript_id})).mappings().all()
    out["guidance"] = [dict(g) for g in grows]
    return out


@router.get("/{symbol}/guidance")
async def guidance_timeline(
    symbol: str,
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(get_current_user),
):
    """Per-company guidance timeline across calls (newest first). Factual:
    management's own stated targets with verbatim-anchored quotes and
    computed change verdicts — no forward view of ours."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = (await session.execute(text("""
            SELECT g.id, g.transcript_id, g.announced_at, g.metric, g.period,
                   g.value_low, g.value_high, g.unit, g.change_verdict,
                   g.prior_guidance_id
            FROM transcript_guidance g
            WHERE g.symbol = :sym
            ORDER BY g.announced_at DESC, g.quote_start
            LIMIT :limit
        """), {"sym": symbol.upper(), "limit": limit})).mappings().all()
    return {"items": [dict(r) | {"announced_at": r["announced_at"].isoformat()}
                      for r in rows],
            "count": len(rows)}


@router.get("/{symbol}")
async def transcripts_for_symbol(
    symbol: str,
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    """Per-company transcript history (metadata only; fetch text by id)."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = (await session.execute(text(f"""
            SELECT {_LIST_COLS}
            FROM concall_transcripts ct
            LEFT JOIN companies c ON c.symbol = ct.symbol
            WHERE ct.symbol = :sym AND ct.parse_status = 'ok'
            ORDER BY ct.announced_at DESC
            LIMIT :limit
        """), {"sym": symbol.upper(), "limit": limit})).all()
    return {"items": [_list_row(r) for r in rows], "count": len(rows)}

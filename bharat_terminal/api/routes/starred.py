"""Account-level starred events (feed shortlist), backed by starred_events.

GET returns full impact reports via the same serializer as /impact — which
deliberately omits trade_signals (SEBI RA scope). Do not serialize reports
through any other path here.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from bharat_terminal.api.auth import get_current_user
from bharat_terminal.api.db import get_session_factory
from bharat_terminal.api.routes.impact import _row_to_impact_report

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/starred", tags=["starred"])

MAX_STARRED = 500


@router.get("/")
async def get_starred(user: dict = Depends(get_current_user)):
    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = await session.execute(
            text("""
                SELECT
                    ir.id, ir.news_id, ir.relevant, ir.confidence,
                    ir.macro_theme, ir.affected_sectors, ir.company_impacts,
                    ir.processing_latency_ms, ir.created_at,
                    ni.source, ni.timestamp_utc, ni.headline, ni.body,
                    ni.url, ni.ingest_latency_ms, ni.category, ni.symbols_mentioned,
                    se.starred_at
                FROM starred_events se
                JOIN impact_reports ir ON ir.id = se.impact_report_id
                LEFT JOIN news_items ni ON ni.id = ir.news_id
                WHERE se.user_id = :uid
                ORDER BY se.starred_at DESC
                LIMIT :lim
            """),
            {"uid": int(user["id"]), "lim": MAX_STARRED},
        )
        records = rows.fetchall()

    items = []
    for r in records:
        item = _row_to_impact_report(r)
        item["starred_at"] = r.starred_at.isoformat() if r.starred_at else None
        items.append(item)
    return {"items": items, "count": len(items)}


@router.put("/{report_id}")
async def star_event(report_id: str, user: dict = Depends(get_current_user)):
    session_factory = get_session_factory()
    try:
        async with session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO starred_events (user_id, impact_report_id)
                    VALUES (:uid, :rid)
                    ON CONFLICT (user_id, impact_report_id) DO NOTHING
                """),
                {"uid": int(user["id"]), "rid": report_id},
            )
            await session.commit()
    except IntegrityError:
        # FK violation: the report id doesn't exist.
        raise HTTPException(status_code=404, detail=f"Unknown report id: {report_id}")
    return {"ok": True, "id": report_id}


@router.delete("/{report_id}")
async def unstar_event(report_id: str, user: dict = Depends(get_current_user)):
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            text(
                "DELETE FROM starred_events "
                "WHERE user_id = :uid AND impact_report_id = :rid"
            ),
            {"uid": int(user["id"]), "rid": report_id},
        )
        await session.commit()
    return {"ok": True, "removed": result.rowcount}

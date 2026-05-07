"""
DB helpers for the research module: read company context + universe, write
research outputs.

Async SQLAlchemy session per call (research jobs are batch-like, low QPS).
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional

from sqlalchemy import select, delete, and_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bharat_terminal.kb.models import (
    Base,
    Company,
    EventDegree,
    EventSectorEdge,
    EventTransmission,
    FundamentalExposure,
    MacroEvent,
    SectorLeader,
    Trigger,
    TriggerBeta,
    UniverseMembership,
)

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://bharat:bharat@postgres:5432/bharat_terminal",
)

_engine = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def _get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    if _sessionmaker is None:
        _engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    sm = _get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Reads ───────────────────────────────────────────────────────────────

async def get_company_context(symbol: str) -> Optional[Dict[str, Any]]:
    """Return the same structure as the KB API's /company/{symbol} response."""
    async with session_scope() as s:
        c = (await s.execute(select(Company).where(Company.symbol == symbol))).scalar_one_or_none()
        if not c:
            return None
        return c.to_dict()


async def list_universe(universe_name: str) -> List[str]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(UniverseMembership.symbol)
                .where(UniverseMembership.universe_name == universe_name)
                .where(UniverseMembership.removed_at.is_(None))
            )
        ).all()
        return [r[0] for r in rows]


# ── Seeding (idempotent upserts) ────────────────────────────────────────

async def upsert_triggers(triggers: List[Dict[str, Any]]) -> int:
    if not triggers:
        return 0
    async with session_scope() as s:
        stmt = pg_insert(Trigger.__table__).values(triggers)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "label": stmt.excluded.label,
                "kind": stmt.excluded.kind,
                "unit": stmt.excluded.unit,
                "data_source": stmt.excluded.data_source,
                "description": stmt.excluded.description,
            },
        )
        await s.execute(stmt)
    return len(triggers)


async def upsert_macro_events(events: List[Dict[str, Any]]) -> int:
    if not events:
        return 0
    async with session_scope() as s:
        stmt = pg_insert(MacroEvent.__table__).values(events)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "label": stmt.excluded.label,
                "trigger_id": stmt.excluded.trigger_id,
                "trigger_spec": stmt.excluded.trigger_spec,
                "propagation_chain": stmt.excluded.propagation_chain,
                "notes": stmt.excluded.notes,
            },
        )
        await s.execute(stmt)
    return len(events)


async def upsert_event_degrees(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    async with session_scope() as s:
        stmt = pg_insert(EventDegree.__table__).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id", "degree"],
            set_={
                "label": stmt.excluded.label,
                "transmission_channel": stmt.excluded.transmission_channel,
            },
        )
        await s.execute(stmt)
    return len(rows)


async def upsert_event_sector_edges(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    async with session_scope() as s:
        stmt = pg_insert(EventSectorEdge.__table__).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id", "degree", "sector"],
            set_={
                "direction": stmt.excluded.direction,
                "magnitude_prior": stmt.excluded.magnitude_prior,
                "lag_text": stmt.excluded.lag_text,
                "lag_min_days": stmt.excluded.lag_min_days,
                "lag_max_days": stmt.excluded.lag_max_days,
                "mechanism": stmt.excluded.mechanism,
            },
        )
        await s.execute(stmt)
    return len(rows)


async def upsert_event_transmissions(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    async with session_scope() as s:
        stmt = pg_insert(EventTransmission.__table__).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id", "from_degree", "to_degree", "gating_trigger_id"],
            set_={
                "confirms_threshold": stmt.excluded.confirms_threshold,
                "refutes_threshold": stmt.excluded.refutes_threshold,
                "description": stmt.excluded.description,
            },
        )
        await s.execute(stmt)
    return len(rows)


async def upsert_universe(universe_name: str, symbols: List[str]) -> int:
    if not symbols:
        return 0
    rows = [{"universe_name": universe_name, "symbol": s} for s in symbols]
    async with session_scope() as s:
        stmt = pg_insert(UniverseMembership.__table__).values(rows)
        stmt = stmt.on_conflict_do_nothing(index_elements=["universe_name", "symbol"])
        await s.execute(stmt)
    return len(rows)


# ── Research-output writes ──────────────────────────────────────────────

async def upsert_trigger_beta(
    *,
    symbol: str,
    trigger_id: str,
    beta: float,
    derivation_method: str,
    confidence: str,
    rationale: Optional[str] = None,
    citations: Optional[list] = None,
    beta_low: Optional[float] = None,
    beta_high: Optional[float] = None,
    model_used: Optional[str] = None,
    refresh_days: int = 90,
) -> None:
    expires = datetime.now(timezone.utc) + timedelta(days=refresh_days)
    row = {
        "symbol": symbol,
        "trigger_id": trigger_id,
        "beta": beta,
        "beta_low": beta_low,
        "beta_high": beta_high,
        "derivation_method": derivation_method,
        "confidence": confidence,
        "rationale": rationale,
        "citations": citations,
        "model_used": model_used,
        "calibrated_at": datetime.now(timezone.utc),
        "expires_at": expires,
    }
    async with session_scope() as s:
        stmt = pg_insert(TriggerBeta.__table__).values([row])
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "trigger_id", "derivation_method"],
            set_={
                "beta": stmt.excluded.beta,
                "beta_low": stmt.excluded.beta_low,
                "beta_high": stmt.excluded.beta_high,
                "confidence": stmt.excluded.confidence,
                "rationale": stmt.excluded.rationale,
                "citations": stmt.excluded.citations,
                "model_used": stmt.excluded.model_used,
                "calibrated_at": stmt.excluded.calibrated_at,
                "expires_at": stmt.excluded.expires_at,
            },
        )
        await s.execute(stmt)


async def upsert_fundamental_exposure(
    *,
    symbol: str,
    trigger_id: str,
    exposure_kind: str,
    mechanism: str,
    pct_of_total: Optional[float] = None,
    total_basis: Optional[str] = None,
    source_doc_url: Optional[str] = None,
    source_period: Optional[str] = None,
    source_kind: Optional[str] = None,
    citations: Optional[list] = None,
) -> None:
    row = {
        "symbol": symbol,
        "trigger_id": trigger_id,
        "exposure_kind": exposure_kind,
        "pct_of_total": pct_of_total,
        "total_basis": total_basis,
        "mechanism": mechanism,
        "source_doc_url": source_doc_url,
        "source_period": source_period,
        "source_kind": source_kind,
        "citations": citations,
    }
    async with session_scope() as s:
        stmt = pg_insert(FundamentalExposure.__table__).values([row])
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "trigger_id", "source_period"],
            set_={
                "exposure_kind": stmt.excluded.exposure_kind,
                "pct_of_total": stmt.excluded.pct_of_total,
                "total_basis": stmt.excluded.total_basis,
                "mechanism": stmt.excluded.mechanism,
                "source_doc_url": stmt.excluded.source_doc_url,
                "source_kind": stmt.excluded.source_kind,
                "citations": stmt.excluded.citations,
            },
        )
        await s.execute(stmt)


async def get_best_trigger_beta(symbol: str, trigger_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the highest-priority TriggerBeta row for (symbol, trigger), or None.
    Priority: regression > agent_research > manual.

    Strict-mode contract: callers (signal generator) drop the symbol from the
    impact graph if this returns None.
    """
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(TriggerBeta).where(
                    and_(
                        TriggerBeta.symbol == symbol,
                        TriggerBeta.trigger_id == trigger_id,
                    )
                )
            )
        ).scalars().all()
    if not rows:
        return None
    priority = {"regression": 0, "agent_research": 1, "manual": 2}
    rows = sorted(rows, key=lambda r: priority.get(r.derivation_method, 99))
    r = rows[0]
    return {
        "symbol": r.symbol,
        "trigger_id": r.trigger_id,
        "beta": r.beta,
        "beta_low": r.beta_low,
        "beta_high": r.beta_high,
        "confidence": r.confidence,
        "derivation_method": r.derivation_method,
        "rationale": r.rationale,
        "citations": r.citations,
        "model_used": r.model_used,
        "calibrated_at": r.calibrated_at,
    }

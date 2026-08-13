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

from sqlalchemy import select, and_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bharat_terminal.kb.models import (
    Company,
    CompanyDossier,
    DossierLinkage,
    DossierSensitivity,
    EventDegree,
    EventSectorEdge,
    EventTransmission,
    FundamentalExposure,
    MacroEvent,
    Trigger,
    TriggerBeta,
    UniverseMembership,
)

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://bharat:bharat@postgres:5432/bharat_terminal",
)

# Per-statement ceiling. asyncpg has NO default command timeout, so a query on
# a connection that dies mid-flight (pool_pre_ping only guards CHECKOUT) awaits
# forever with no exception, no log, and no server-side trace — kb-monitor's
# memory_index loop hung exactly this way for 14h on 2026-08-13. With the
# ceiling the await raises, the loop's except logs it, and the next cycle
# recovers on a fresh connection. Biggest legitimate statement observed (the
# 120-day impact_reports scan) runs ~25s; 120s is 5x headroom.
DB_COMMAND_TIMEOUT_S = float(os.getenv("DB_COMMAND_TIMEOUT_S", "120"))

_engine = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def _get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    if _sessionmaker is None:
        _engine = create_async_engine(
            DATABASE_URL, echo=False, pool_pre_ping=True, pool_recycle=1800,
            connect_args={"command_timeout": DB_COMMAND_TIMEOUT_S},
        )
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


# ── Dossier-agent helpers ────────────────────────────────────────────────


async def get_company_name(symbol: str) -> Optional[str]:
    """Return the human-readable company name for context in the prompt."""
    async with session_scope() as s:
        row = (
            await s.execute(select(Company.company_name).where(Company.symbol == symbol))
        ).scalar_one_or_none()
    return row


async def list_triggers() -> List[Dict[str, Any]]:
    """Return all trigger rows for the dossier agent to enumerate against."""
    async with session_scope() as s:
        rows = (await s.execute(select(Trigger))).scalars().all()
    return [
        {
            "id": r.id,
            "label": r.label,
            "kind": r.kind,
            "unit": r.unit,
            "data_source": r.data_source,
        }
        for r in rows
    ]


async def upsert_company_dossier(
    *,
    symbol: str,
    source_quarter: Optional[str],
    refresh_kind: str,
    model_used: str,
    tokens_used: int,
    data_quality: Optional[float],
    business: Optional[dict],
    financials: Optional[dict],
    sensitivities: Optional[list],
    linkages: Optional[list],
    risks: Optional[list],
    catalysts: Optional[list],
    mgmt_commentary: Optional[dict],
    citations: Optional[list],
) -> None:
    """
    Insert-or-update the rich JSONB dossier row. The two flat tables
    (`dossier_sensitivities`, `dossier_linkages`) are populated separately
    by `replace_dossier_*` below, so this only owns `company_dossiers`.
    """
    # Coerce source_quarter ISO string → date
    sq = None
    if source_quarter:
        try:
            sq = datetime.fromisoformat(source_quarter).date()
        except (ValueError, TypeError):
            sq = None

    async with session_scope() as s:
        stmt = pg_insert(CompanyDossier.__table__).values(
            symbol=symbol,
            version="1.0.0",
            source_quarter=sq,
            refresh_kind=refresh_kind,
            model_used=model_used,
            tokens_used=tokens_used,
            data_quality=data_quality,
            business=business,
            financials=financials,
            sensitivities=sensitivities,   # full JSONB blob for traceability
            linkages=linkages,
            risks=risks,
            catalysts=catalysts,
            mgmt_commentary=mgmt_commentary,
            citations=citations,
            generated_at=datetime.now(timezone.utc),
        ).on_conflict_do_update(
            index_elements=["symbol"],
            set_={
                "version": "1.0.0",
                "source_quarter": sq,
                "refresh_kind": refresh_kind,
                "model_used": model_used,
                "tokens_used": tokens_used,
                "data_quality": data_quality,
                "business": business,
                "financials": financials,
                "sensitivities": sensitivities,
                "linkages": linkages,
                "risks": risks,
                "catalysts": catalysts,
                "mgmt_commentary": mgmt_commentary,
                "citations": citations,
                "generated_at": datetime.now(timezone.utc),
            },
        )
        await s.execute(stmt)


async def replace_dossier_sensitivities(symbol: str, rows: List[Dict[str, Any]]) -> None:
    """
    Replace all sensitivity rows for a symbol atomically (delete + bulk insert).
    Skips rows whose trigger_id is unknown so a stale trigger reference doesn't
    abort the whole save.
    """
    if not rows:
        return
    async with session_scope() as s:
        # Find which trigger_ids are valid
        valid_triggers = set(
            (await s.execute(select(Trigger.id))).scalars().all()
        )
        rows_valid = []
        for r in rows:
            if r["trigger_id"] not in valid_triggers:
                logger.warning(
                    f"[DOSSIER] {symbol}: skipping sensitivity for unknown "
                    f"trigger_id={r['trigger_id']!r} (not in triggers table)"
                )
                continue
            rows_valid.append(r)

        await s.execute(
            DossierSensitivity.__table__.delete().where(
                DossierSensitivity.symbol == symbol
            )
        )
        if rows_valid:
            await s.execute(
                DossierSensitivity.__table__.insert(),
                [
                    {
                        "symbol": symbol,
                        "trigger_id": r["trigger_id"],
                        "beta": r["beta"],
                        "channel": r["channel"],
                        "lag_days": r["lag_days"],
                        "confidence": r["confidence"],
                        "rationale": r.get("rationale"),
                        "evidence_url": r.get("evidence_url"),
                    }
                    for r in rows_valid
                ],
            )


async def replace_dossier_linkages(symbol: str, rows: List[Dict[str, Any]]) -> None:
    """
    Replace all linkage rows for a symbol atomically. Skips rows whose
    target_symbol doesn't exist in companies — but only with a warning;
    we DO keep them by default since the target may be tracked outside our
    universe (or about to be added).
    """
    if not rows:
        return
    async with session_scope() as s:
        # Probe which target_symbols are known; we keep all but tag unknowns.
        known = set(
            (
                await s.execute(
                    select(Company.symbol).where(
                        Company.symbol.in_({r["target_symbol"] for r in rows})
                    )
                )
            ).scalars().all()
        )
        for r in rows:
            if r["target_symbol"] not in known:
                logger.info(
                    f"[DOSSIER] {symbol}: linkage target {r['target_symbol']!r} "
                    f"not in companies table — keeping anyway"
                )

        await s.execute(
            DossierLinkage.__table__.delete().where(
                DossierLinkage.source_symbol == symbol
            )
        )
        # Insert only rows where the target IS known (FK constraint requires it)
        rows_with_fk = [r for r in rows if r["target_symbol"] in known]
        if rows_with_fk:
            await s.execute(
                DossierLinkage.__table__.insert(),
                [
                    {
                        "source_symbol": symbol,
                        "target_symbol": r["target_symbol"],
                        "kind": r["kind"],
                        "strength": r["strength"],
                        "direction": r["direction"],
                        "propagation_lag_days": r["propagation_lag_days"],
                        "rationale": r.get("rationale"),
                        "evidence_url": r.get("evidence_url"),
                    }
                    for r in rows_with_fk
                ],
            )

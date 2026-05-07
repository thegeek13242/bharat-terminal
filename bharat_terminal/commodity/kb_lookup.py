"""
Read-side: pull (commodity → ranked exposures) from the KB tables populated
by the research agent + (eventually) the regression calibrator.

Strict-mode contract: a symbol with no `trigger_betas` row for the commodity
is DROPPED from the impact graph. There is no hand-tuned fallback.

Cached for 5 minutes per (event_id, trigger_id) since exposures don't move
on a tick-by-tick basis.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bharat_terminal.kb.models import (
    Company,
    EventSectorEdge,
    FundamentalExposure,
    TriggerBeta,
)

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://bharat:bharat@postgres:5432/bharat_terminal",
)

_engine = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def _sm() -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    if _sessionmaker is None:
        _engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _sessionmaker


@dataclass(frozen=True)
class ResolvedExposure:
    symbol: str
    company_name: str
    sector: str
    beta: float
    confidence: str
    derivation_method: str
    rationale: Optional[str]
    citations: Optional[list]


_PRIORITY = {"regression": 0, "agent_research": 1, "manual": 2}

# (event_id, trigger_id) → (epoch_at, list)
_CACHE: Dict[Tuple[str, str], Tuple[float, List[ResolvedExposure]]] = {}
_CACHE_TTL_SECONDS = 300.0


async def resolve_exposures(
    *,
    event_id: str,
    trigger_id: str,
    universe: Optional[List[str]] = None,
) -> List[ResolvedExposure]:
    """
    Returns the ranked list of (symbol, beta, …) for the given trigger,
    intersected with the event's sector edges (so we only rank companies
    whose sector is structurally implicated).

    If `universe` is provided, results are restricted to those symbols.
    """
    cache_key = (event_id, trigger_id)
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        out = cached[1]
        if universe is None:
            return out
        uset = set(universe)
        return [e for e in out if e.symbol in uset]

    sm = _sm()
    async with sm() as session:
        # Pull all betas for this trigger (any derivation method).
        beta_rows = (
            await session.execute(
                select(TriggerBeta).where(TriggerBeta.trigger_id == trigger_id)
            )
        ).scalars().all()
        if not beta_rows:
            _CACHE[cache_key] = (now, [])
            return []

        # Pick best beta per symbol by derivation priority.
        best: Dict[str, TriggerBeta] = {}
        for r in beta_rows:
            cur = best.get(r.symbol)
            if cur is None or _PRIORITY.get(r.derivation_method, 99) < _PRIORITY.get(cur.derivation_method, 99):
                best[r.symbol] = r

        # Pull sector edges for the event so we can scope to structurally
        # implicated sectors. If the event has no edges (e.g. agent has not
        # been seeded yet), accept all sectors.
        sector_rows = (
            await session.execute(
                select(EventSectorEdge.sector).where(EventSectorEdge.event_id == event_id)
            )
        ).all()
        relevant_sectors = {row[0] for row in sector_rows} if sector_rows else None

        # Join to companies for sector + name; drop symbols not in `companies`.
        comps = (
            await session.execute(
                select(Company.symbol, Company.company_name, Company.sector_nse)
                .where(Company.symbol.in_(best.keys()))
            )
        ).all()
        comp_map = {sym: (name, sector) for (sym, name, sector) in comps}

    out: List[ResolvedExposure] = []
    for symbol, beta_row in best.items():
        meta = comp_map.get(symbol)
        if not meta:
            continue
        name, sector = meta
        if relevant_sectors is not None and sector and sector not in relevant_sectors:
            # Soft skip — sector matching is loose; if the company sector
            # doesn't appear in the event-edge list we don't rank it. This
            # is the strictness the user asked for.
            continue
        out.append(
            ResolvedExposure(
                symbol=symbol,
                company_name=name or symbol,
                sector=sector or "",
                beta=float(beta_row.beta),
                confidence=beta_row.confidence,
                derivation_method=beta_row.derivation_method,
                rationale=beta_row.rationale,
                citations=beta_row.citations,
            )
        )

    # Rank by |beta| descending (largest sensitivity first)
    out.sort(key=lambda e: abs(e.beta), reverse=True)

    _CACHE[cache_key] = (now, out)
    if universe is None:
        return out
    uset = set(universe)
    return [e for e in out if e.symbol in uset]


def invalidate_cache() -> None:
    _CACHE.clear()

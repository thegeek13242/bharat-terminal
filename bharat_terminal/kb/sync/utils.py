"""
Shared utilities for KB sync pipelines.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://bharat:bharat@localhost:5432/bharat_terminal")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


# One engine (= one connection pool) per event loop, not per call.
#
# WHY (2026-08-01 postmortem): this function used to build a NEW engine on
# every call, and several sync jobs call it inside a per-symbol loop —
# bse_shareholding_sync.upsert_rows() does it once per scrip across a
# 1,631-name universe. Each engine holds up to pool_size+max_overflow
# connections and nothing disposed them, so the Saturday sweep chain walked
# Postgres to its 100-connection ceiling (kb-monitor alone held 72), collapsed
# the papertrade pool, and starved its own later stages — 734/1,631 BSE
# shareholding scrips failed that night. Sharing one pool makes the call
# cheap and bounded no matter how often it is called.
#
# Keyed by running event loop: asyncpg connections belong to the loop that
# created them, so an engine must never be reused across loops (CLI jobs that
# call asyncio.run() more than once). With no running loop we fall back to the
# old per-call behaviour — that path is one-shot and never the leak.
_ENGINE_CACHE: dict = {}


def create_async_session() -> async_sessionmaker[AsyncSession]:
    import asyncio
    try:
        key = id(asyncio.get_running_loop())
    except RuntimeError:
        key = None

    if key is not None and key in _ENGINE_CACHE:
        return _ENGINE_CACHE[key][1]

    engine = create_async_engine(
        DATABASE_URL, echo=False, pool_size=5, max_overflow=10,
        # A now long-lived shared pool must not hand out connections the
        # server has already dropped.
        pool_recycle=1800, pool_pre_ping=True,
    )
    factory = async_sessionmaker(engine, class_=AsyncSession,
                                 expire_on_commit=False)
    if key is not None:
        _ENGINE_CACHE[key] = (engine, factory)
    return factory


async def dispose_engines() -> None:
    """Close pooled connections for the current loop (shutdown paths/tests)."""
    import asyncio
    try:
        key = id(asyncio.get_running_loop())
    except RuntimeError:
        return
    entry = _ENGINE_CACHE.pop(key, None)
    if entry is not None:
        await entry[0].dispose()


async def start_sync_log(session: AsyncSession, sync_type: str, source: str) -> int:
    """Create a sync_log entry and return its ID."""
    from bharat_terminal.kb.models import SyncLog
    log = SyncLog(sync_type=sync_type, source=source, status="running")
    session.add(log)
    await session.flush()
    return log.id


async def complete_sync_log(
    session: AsyncSession,
    log_id: int,
    status: str,
    records_processed: int = 0,
    records_failed: int = 0,
    error_message: Optional[str] = None,
    metadata: Optional[dict] = None,
):
    from bharat_terminal.kb.models import SyncLog
    from sqlalchemy import select
    result = await session.execute(select(SyncLog).where(SyncLog.id == log_id))
    log = result.scalar_one_or_none()
    if log:
        log.status = status
        log.completed_at = datetime.now(timezone.utc)
        log.records_processed = records_processed
        log.records_failed = records_failed
        log.error_message = error_message
        log.metadata_json = metadata


def compute_data_quality(company, has_quarterly_data: bool = False, has_relationships: bool = False) -> float:
    """
    Weighted score (0.0-1.0) based on field population.
    0.1 = symbol+name only (stub)
    1.0 = fully populated with verified data
    """
    score = 0.1  # base: symbol + name exist

    # Financials: +0.25
    financials_fields = [company.revenue_ttm_cr, company.pat_cr, company.eps_ttm, company.ebitda_margin_pct]
    filled = sum(1 for f in financials_fields if f is not None)
    score += 0.25 * (filled / len(financials_fields))

    # Ratios: +0.15
    ratio_fields = [company.pe_ratio, company.pb_ratio, company.roe_pct]
    filled = sum(1 for f in ratio_fields if f is not None)
    score += 0.15 * (filled / len(ratio_fields))

    # Description: +0.1
    if company.description_200w:
        score += 0.1

    # Quarterly data in financial_results table: +0.2
    if has_quarterly_data:
        score += 0.2

    # DCF: +0.1
    if company.fair_value_per_share is not None:
        score += 0.1

    # Relationships: +0.1
    if has_relationships:
        score += 0.1

    return min(round(score, 2), 1.0)


async def invalidate_company_cache(symbol: str):
    """Delete company:{symbol} from Redis after data updates."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(REDIS_URL)
        await r.delete(f"company:{symbol}")
        await r.close()
    except Exception as e:
        logger.debug(f"Redis cache invalidation failed for {symbol}: {e}")

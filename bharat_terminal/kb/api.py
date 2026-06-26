"""
FastAPI REST API for the knowledge base.
GET /company/{symbol}   -> CompanyProfile + DCF (<=200ms, Redis cache 6h TTL)
GET /graph/{symbol}     -> Company relationship graph
GET /health             -> Health check
"""
import logging
import os
import json
from typing import Optional
from datetime import datetime

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select, and_

from bharat_terminal.kb.models import Company, CompanyRelationship
from bharat_terminal.kb.company_memory import build_company_memory
from bharat_terminal.grounding.queries import get_price_returns
from bharat_terminal.kb.live_price import get_current_ltps

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://bharat:bharat@postgres:5432/bharat_terminal")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CACHE_TTL = 6 * 3600  # 6 hours
MEMORY_CACHE_TTL = 15 * 60  # 15 min — memory shifts as fresh news flows
PRICE_RETURNS_CACHE_TTL = 15 * 60  # prices are EOD-daily; short cache is plenty

app = FastAPI(title="Bharat Terminal KB API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Database engine
engine = create_async_engine(DATABASE_URL, pool_size=10, max_overflow=20, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Redis client
redis_client: Optional[aioredis.Redis] = None


@app.on_event("startup")
async def startup():
    global redis_client
    redis_client = await aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    logger.info("KB API started. DB and Redis connected.")


@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.close()
    await engine.dispose()


@app.get("/health")
async def health():
    return {"status": "ok", "service": "kb-api", "timestamp": datetime.utcnow().isoformat()}


@app.get("/company/{symbol}")
async def get_company(symbol: str):
    """
    Get full company profile including DCF model.
    Response time SLA: <=200ms (Redis cache hit ~5ms, DB hit ~50-100ms).
    Cache TTL: 6 hours.
    """
    symbol = symbol.upper().strip()
    cache_key = f"company:{symbol}"

    # Try cache first
    if redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"Redis read failed for {symbol}: {e}")

    # Query database
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(
                and_(Company.symbol == symbol, Company.is_active == True)
            )
        )
        company = result.scalar_one_or_none()

        if not company:
            # Try case-insensitive search by company name
            result = await session.execute(
                select(Company).where(Company.company_name.ilike(f"%{symbol}%"))
                .limit(1)
            )
            company = result.scalar_one_or_none()

        if not company:
            raise HTTPException(status_code=404, detail=f"Company {symbol} not found")

        data = company.to_dict()

    # Cache the result
    if redis_client:
        try:
            await redis_client.setex(cache_key, CACHE_TTL, json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"Redis write failed for {symbol}: {e}")

    return data


@app.get("/company/{symbol}/memory")
async def get_company_memory(symbol: str):
    """Bounded, recency-decayed event memory for a company (derived from
    impact_reports). Used by the analysis agents for event context. 15-min cache."""
    symbol = symbol.upper().strip()
    cache_key = f"memory:{symbol}"

    if redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"Redis read failed for memory:{symbol}: {e}")

    async with AsyncSessionLocal() as session:
        data = await build_company_memory(session, symbol)

    if redis_client:
        try:
            await redis_client.setex(cache_key, MEMORY_CACHE_TTL, json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"Redis write failed for memory:{symbol}: {e}")

    return data


@app.get("/company/{symbol}/price-returns")
async def get_company_price_returns(symbol: str, windows: str = Query("1,3,5,30")):
    """Trailing price returns (+ volatility / drawdown) for a symbol from
    `price_points`, via grounding.get_price_returns. Backs the analysis
    'priced-in' check (short windows like 1/3/5d). Short cache; fail-soft —
    callers treat any error/empty payload as 'unknown' and skip downgrades."""
    symbol = symbol.upper().strip()
    try:
        wins = tuple(int(w) for w in windows.split(",") if w.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="windows must be comma-separated integers")
    if not wins:
        wins = (1, 3, 5, 30)
    cache_key = f"pricereturns:{symbol}:{windows}"

    if redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"Redis read failed for {cache_key}: {e}")

    data = await get_price_returns(symbol, windows=wins)

    # Default recent move = shortest available trailing return (daily, EOD).
    tr = (data.get("trailing_returns_pct") or {}) if isinstance(data, dict) else {}
    data["recent_move_pct"] = next((tr[k] for k in ("1d", "3d", "5d") if tr.get(k) is not None), None)

    # Enrich with the LIVE equity price (Angel One, fail-open) so the priced-in
    # check sees today's intraday move, not just yesterday's close. recent_move
    # becomes (current_ltp / close~3d-ago - 1), combining current + history.
    if not data.get("error"):
        try:
            live = await get_current_ltps([symbol], redis_client)
            info = live.get(symbol.upper())
            latest_close = data.get("latest_close")
            if info and info.get("ltp") and latest_close:
                data["current_ltp"] = round(info["ltp"], 2)
                ret_base = next((tr[k] for k in ("3d", "5d", "1d") if tr.get(k) is not None), 0.0) or 0.0
                baseline = latest_close / (1 + ret_base / 100.0)
                if baseline:
                    data["recent_move_pct"] = round((info["ltp"] / baseline - 1) * 100, 2)
        except Exception as e:
            logger.debug(f"live-price enrich failed for {symbol} (fail-open): {e}")

    if redis_client:
        try:
            await redis_client.setex(cache_key, PRICE_RETURNS_CACHE_TTL, json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"Redis write failed for {cache_key}: {e}")

    return data


@app.get("/graph/{symbol}")
async def get_company_graph(
    symbol: str,
    hops: int = Query(default=1, ge=1, le=2),
):
    """Get company relationship graph up to N hops."""
    symbol = symbol.upper().strip()
    cache_key = f"graph:{symbol}:{hops}"

    if redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    async with AsyncSessionLocal() as session:
        # Hop 0: the company itself
        company_result = await session.execute(
            select(Company).where(Company.symbol == symbol)
        )
        center = company_result.scalar_one_or_none()
        if not center:
            raise HTTPException(status_code=404, detail=f"Company {symbol} not found")

        nodes = {symbol: {"symbol": symbol, "name": center.company_name, "hop": 0}}
        edges = []

        # Hop 1 edges
        rels_result = await session.execute(
            select(CompanyRelationship).where(CompanyRelationship.source_symbol == symbol)
        )
        hop1_rels = rels_result.scalars().all()

        hop1_symbols = set()
        for rel in hop1_rels:
            edges.append({
                "source_symbol": rel.source_symbol,
                "target_symbol": rel.target_symbol,
                "target_name": rel.target_name,
                "relationship_type": rel.relationship_type,
                "weight": rel.weight,
                "hop": 1,
            })
            nodes[rel.target_symbol] = {
                "symbol": rel.target_symbol,
                "name": rel.target_name or rel.target_symbol,
                "hop": 1,
            }
            hop1_symbols.add(rel.target_symbol)

        # Hop 2 edges (if requested)
        if hops >= 2 and hop1_symbols:
            hop2_result = await session.execute(
                select(CompanyRelationship).where(
                    CompanyRelationship.source_symbol.in_(list(hop1_symbols))
                ).limit(50)
            )
            for rel in hop2_result.scalars().all():
                if rel.target_symbol != symbol:  # Avoid pointing back to center
                    edges.append({
                        "source_symbol": rel.source_symbol,
                        "target_symbol": rel.target_symbol,
                        "target_name": rel.target_name,
                        "relationship_type": rel.relationship_type,
                        "weight": rel.weight,
                        "hop": 2,
                    })
                    if rel.target_symbol not in nodes:
                        nodes[rel.target_symbol] = {
                            "symbol": rel.target_symbol,
                            "name": rel.target_name or rel.target_symbol,
                            "hop": 2,
                        }

        graph_data = {
            "symbol": symbol,
            "nodes": list(nodes.values()),
            "edges": edges,
        }

    if redis_client:
        try:
            await redis_client.setex(cache_key, 3600, json.dumps(graph_data))
        except Exception:
            pass

    return graph_data


@app.get("/search")
async def search_companies(
    q: str = Query(..., min_length=2),
    limit: int = Query(default=10, le=50),
):
    """Search companies by name or symbol (used by Agent 2 for entity resolution)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(
                and_(
                    Company.is_active == True,
                    (Company.symbol.ilike(f"%{q}%") | Company.company_name.ilike(f"%{q}%"))
                )
            ).limit(limit)
        )
        companies = result.scalars().all()
        return [
            {"symbol": c.symbol, "name": c.company_name, "sector": c.sector_nse, "mcap_cr": c.mcap_cr}
            for c in companies
        ]


@app.get("/companies")
async def list_companies(
    sector: Optional[str] = None,
    exchange: Optional[str] = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
):
    """List companies with optional filters."""
    async with AsyncSessionLocal() as session:
        query = select(Company).where(Company.is_active == True)
        if sector:
            query = query.where(Company.sector_nse == sector.upper())
        if exchange:
            query = query.where(Company.exchange == exchange.upper())
        query = query.order_by(Company.mcap_cr.desc().nullslast()).limit(limit).offset(offset)
        result = await session.execute(query)
        companies = result.scalars().all()
        return [
            {
                "symbol": c.symbol,
                "name": c.company_name,
                "sector": c.sector_nse,
                "mcap_cr": c.mcap_cr,
                "exchange": c.exchange,
                "pe_ratio": c.pe_ratio,
                "data_quality": c.data_quality_score,
            }
            for c in companies
        ]

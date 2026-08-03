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
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select, and_, text

from bharat_terminal.kb.models import Company, CompanyRelationship
from bharat_terminal.kb.company_memory import build_company_memory
from bharat_terminal.kb.memory.sector import build_sector_memory
from bharat_terminal.kb.memory.market import build_market_memory
from bharat_terminal.kb.memory import core as memory_core
from bharat_terminal.grounding.queries import get_market_state, get_price_returns
from bharat_terminal.kb.live_price import get_current_ltps

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://bharat:bharat@postgres:5432/bharat_terminal")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CACHE_TTL = 6 * 3600  # 6 hours
MEMORY_CACHE_TTL = 15 * 60  # 15 min — memory shifts as fresh news flows
MEMORY_SEMANTIC_ENABLED = os.getenv("MEMORY_SEMANTIC_ENABLED", "1") == "1"
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


@app.get("/quotes/full")
async def get_full_quotes_batch(symbols: str):
    """Batch FULL-mode quotes (ltp, open, prev_close, total buy/sell
    quantities, best bid/ask) via the shared Angel One session. Built for
    the papertrade pre-open demand probe; same licensing posture as /ltp
    (internal-only consumers, never broadcast/stored beyond provenance)."""
    from bharat_terminal.kb.live_price import get_full_quotes
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:16]
    if not syms:
        return {"quotes": {}}
    quotes = await get_full_quotes(syms, redis_client)
    return {"quotes": quotes, "source": "angelone_full",
            "asof": datetime.utcnow().isoformat()}


@app.get("/ltp")
async def get_ltp_batch(symbols: str):
    """Batch live equity LTPs via the shared Angel One session — one upstream
    getMarketData call for all symbols. No EOD fallback: absent/stale session
    returns an empty quotes map and the caller keeps its own pending state.

    LICENSING (do not widen): Angel One data is licensed for the account
    holder's own use. Reachable by customers ONLY through api-gateway routes
    that gate on require_internal_user / INTERNAL_RESEARCH_EMAILS — never
    expose publicly and never fold output into broadcast/stored reports.
    """
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:16]
    if not syms:
        return {"quotes": {}, "source": "angelone_live",
                "asof": datetime.utcnow().isoformat()}
    live = await get_current_ltps(syms, redis_client)
    return {
        "quotes": {s: {"ltp": i["ltp"], "prev_close": i.get("prev_close"),
                       "open": i.get("open"), "exchange": i.get("exchange")}
                   for s, i in live.items() if i.get("ltp")},
        "source": "angelone_live",
        "asof": datetime.utcnow().isoformat(),
    }


@app.get("/ltp/{symbol}")
async def get_ltp(symbol: str):
    """Live NSE equity LTP via the shared Angel One session, falling back to
    the last daily close from the DB when no live session is available.

    LICENSING (do not widen): Angel One data is licensed for the account
    holder's own use. This endpoint is reachable by customers ONLY through the
    api-gateway's require_internal_user gate — never expose it on a public
    route and never fold its output into broadcast/stored reports.
    """
    sym = symbol.upper()
    live = await get_current_ltps([sym], redis_client)
    info = live.get(sym)
    if info and info.get("ltp"):
        return {
            "symbol": sym,
            "ltp": info["ltp"],
            "prev_close": info.get("prev_close"),
            "source": "angelone_live",
            "asof": datetime.utcnow().isoformat(),
        }

    # Fail-open fallback: last daily close (bhavcopy) — honest label, no live.
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            text("SELECT c.last_price, "
                 "       (SELECT max(date) FROM price_points p WHERE p.symbol = c.symbol) AS latest_price_date "
                 "FROM companies c WHERE c.symbol = :s"),
            {"s": sym},
        )).first()
    if not row or row.last_price is None:
        raise HTTPException(status_code=404, detail=f"No price data for {sym}")
    return {
        "symbol": sym,
        "ltp": None,
        "prev_close": float(row.last_price),
        "source": "eod_close",
        "asof": row.latest_price_date.isoformat() if row.latest_price_date else None,
    }


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
        resolved_symbol = company.symbol  # may differ from path (name fallback)

    # Latest exchange surveillance/band state (migration 033). Additive and
    # fail-open: a missing market_state table (pre-migration) or a transient
    # error must not take down the profile — the key is then null, the same
    # shape as "symbol on no surveillance list". Note the 6h profile cache:
    # market_state inside a cached profile can lag the daily sync by up to
    # CACHE_TTL; the embedded as_of keeps that staleness visible.
    try:
        data["market_state"] = await get_market_state(resolved_symbol)
    except Exception as e:
        logger.warning(f"market_state fetch failed for {resolved_symbol} (fail-open): {e}")
        data["market_state"] = None

    # Cache the result
    if redis_client:
        try:
            await redis_client.setex(cache_key, CACHE_TTL, json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"Redis write failed for {symbol}: {e}")

    return data


@app.get("/company/{symbol}/filings/siblings")
async def get_filing_siblings(symbol: str, before_ts: str,
                              exclude_id: str = "", hours: float = 2.0,
                              max_chars: int = 20000):
    """C1b — same-symbol exchange filings in the window before a trigger
    filing (the results cluster: table → press release → presentation).
    Consumed by analysis stage-3 for sibling context (STAGE3_SIBLING_CONTEXT)
    and by research replay. No cache: the window is a point-in-time query."""
    symbol = symbol.upper().strip()
    hours = max(0.1, min(hours, 24.0))
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            text("""
                SELECT id, headline,
                       left(coalesce(body, ''), :max_chars) AS body,
                       timestamp_utc
                FROM news_items
                WHERE source IN ('NSE_FILINGS', 'BSE_FILINGS')
                  AND symbols_mentioned ? :symbol
                  AND timestamp_utc >= CAST(:before_ts AS timestamptz)
                                       - make_interval(secs => :window_s)
                  AND timestamp_utc <= CAST(:before_ts AS timestamptz)
                  AND id <> :exclude_id
                  AND char_length(coalesce(body, '')) > 500
                ORDER BY timestamp_utc DESC
                LIMIT 4
            """),
            {"symbol": symbol, "before_ts": before_ts,
             "window_s": hours * 3600.0, "exclude_id": exclude_id,
             "max_chars": max(1000, min(max_chars, 60000))},
        )).mappings().all()
    return {"symbol": symbol, "siblings": [dict(r) for r in rows]}


@app.get("/company/{symbol}/guidance")
async def get_company_guidance(symbol: str):
    """C2 — standing guidance state + delivered-vs-guided outcomes +
    credibility status (INTERNAL, pipeline-facing; the customer surface is
    the gateway's /transcripts/{symbol}/guidance). Derived from
    guidance_ledger/guidance_outcomes (migration 034 + guidance_ledger_sync).
    Credibility is computed here at read time and reports
    'insufficient_history' below 2 resolved outcomes — by design it will say
    that for almost every company until FY27 actuals land (Apr-May 2027)."""
    symbol = symbol.upper().strip()
    cache_key = f"guidance:{symbol}"
    if redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"Redis read failed for {cache_key}: {e}")

    async with AsyncSessionLocal() as session:
        try:
            standing = (await session.execute(
                text("""
                    SELECT metric_key, metric_raw, period_key, period_end,
                           value_low, value_high, unit_key, better_direction,
                           change_verdict, restatements, stated_at, scoreable
                    FROM guidance_ledger
                    WHERE symbol = :symbol AND status = 'active'
                    ORDER BY stated_at DESC, metric_key
                    LIMIT 60
                """), {"symbol": symbol})).mappings().all()
            outcomes = (await session.execute(
                text("""
                    SELECT metric_key, period_key, guided_low, guided_high,
                           delivered, delivered_verdict, met,
                           basis_consolidated, formula, actual_period_end,
                           ambiguity_flags
                    FROM guidance_outcomes
                    WHERE symbol = :symbol
                    ORDER BY actual_period_end DESC
                    LIMIT 40
                """), {"symbol": symbol})).mappings().all()
        except Exception as e:
            # Pre-migration-034 deployments: report absence, don't 500.
            logger.warning(f"guidance tables unavailable: {e}")
            return {"symbol": symbol, "standing": [], "outcomes": [],
                    "credibility": {"status": "unavailable"}}

    resolved = [o for o in outcomes if o["met"] is not None]
    n_met = sum(1 for o in resolved if o["met"])
    credibility = {
        "n_resolved": len(resolved), "n_met": n_met,
        "hit_rate": (round(n_met / len(resolved), 3) if resolved else None),
        # Multiplier gate (MANAGEMENT_COMMENTARY_2026-07-31.md §C2): no
        # score is usable below 2 resolved pairs.
        "status": ("ok" if len(resolved) >= 2 else "insufficient_history"),
    }
    data = {"symbol": symbol,
            "standing": [dict(r) for r in standing],
            "outcomes": [dict(r) for r in outcomes],
            "credibility": credibility}
    if redis_client:
        try:
            await redis_client.setex(cache_key, CACHE_TTL,
                                     json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"Redis write failed for {cache_key}: {e}")
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


# ── Sector / market / combined event-memory tiers ────────────────────────────
class MemoryContextQuery(BaseModel):
    """The current news context, used to relevance-rank memory retrieval."""
    headline: Optional[str] = None
    macro_theme: Optional[str] = None
    sectors: Optional[list] = None
    symbols: Optional[list] = None
    include_company: bool = True


async def _fetch_digest(session, scope_type: str, scope_key: str):
    """Attach the cached LLM narrative digest if one exists (else None)."""
    row = (await session.execute(text(
        "SELECT digest_text, net_tilt, generated_at FROM memory_digests "
        "WHERE scope_type = :t AND scope_key = :k"
    ), {"t": scope_type, "k": scope_key})).first()
    if not row or not row.digest_text:
        return None
    return {"text": row.digest_text, "net_tilt": row.net_tilt,
            "generated_at": row.generated_at.isoformat() if row.generated_at else None}


async def _embed_context(headline: Optional[str], macro_theme: Optional[str]):
    """Embed the incoming news once (server-side) for relevance retrieval.
    Fail-soft: returns None (→ recency/importance ranking) if disabled or erroring."""
    if not MEMORY_SEMANTIC_ENABLED:
        return None
    q = memory_core.embed_text_for(headline, macro_theme, None)
    if not q:
        return None
    try:
        from bharat_terminal.llm.embeddings import embed_query
        return await embed_query(q)
    except Exception as e:
        logger.warning(f"memory context embed failed: {e}")
        return None


@app.get("/sector/{sector}/memory")
async def get_sector_memory(sector: str):
    """Sector-tier event memory (no relevance ranking — debug/manual use)."""
    async with AsyncSessionLocal() as session:
        data = await build_sector_memory(session, sector)
        data["digest"] = await _fetch_digest(session, "sector", sector.strip().upper())
    return data


@app.get("/market/memory")
async def get_market_memory():
    """Market-tier event memory (no relevance ranking — debug/manual use)."""
    async with AsyncSessionLocal() as session:
        data = await build_market_memory(session)
        data["digest"] = await _fetch_digest(session, "market", "MARKET")
    return data


@app.post("/memory/context")
async def memory_context(q: MemoryContextQuery):
    """Combined market + sector + company memory for one news item, relevance-
    ranked to the supplied context. Embeds the news ONCE and fans out to every
    tier, so the analysis pipeline needs a single round-trip per item."""
    qvec = await _embed_context(q.headline, q.macro_theme)
    sectors = [s.strip().upper() for s in (q.sectors or []) if s and s.strip()]
    symbols = [s.strip().upper() for s in (q.symbols or []) if s and s.strip()]
    async with AsyncSessionLocal() as session:
        market = await build_market_memory(
            session, query_embedding=qvec, query_theme=q.macro_theme, query_sectors=sectors)
        market["digest"] = await _fetch_digest(session, "market", "MARKET")

        sec_out = {}
        for sec in sectors[:6]:
            m = await build_sector_memory(
                session, sec, query_embedding=qvec, query_theme=q.macro_theme)
            m["digest"] = await _fetch_digest(session, "sector", sec)
            sec_out[sec] = m

        comp_out = {}
        if q.include_company:
            for sym in symbols[:8]:
                comp_out[sym] = await build_company_memory(
                    session, sym, query_embedding=qvec,
                    query_theme=q.macro_theme, query_sectors=sectors)

    return {"market": market, "sectors": sec_out, "companies": comp_out,
            "semantic": qvec is not None}


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

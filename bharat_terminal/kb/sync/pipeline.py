"""
Pipeline orchestrator for Bharat Terminal knowledge base population.

Coordinates all sync phases:
  1. sync_company_master  — upsert NSE equity master into companies table
  2. sync_financials      — fetch NSE XBRL filings, parse, store quarterly P&L
  3. sync_dcf_all         — recompute DCF for enriched companies
  4. sync_prices          — backfill daily OHLCV from bhavcopy
  5. sync_all             — run all phases sequentially

Primary data source: NSE XBRL filings (via corporates-financial-results API).
Secondary verification: Screener.in (TODO: cross-validation pass).
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import select, func as sa_func, or_, exists
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bharat_terminal.kb.models import (
    Company, NseFiling, FinancialResult, PricePoint, SyncLog,
)
from bharat_terminal.kb.sync.nse_financial_results import (
    create_nse_client,
    refresh_nse_cookies,
    fetch_filing_index,
    parse_filing_metadata,
    filter_consolidated_quarterly,
    fetch_and_parse_xbrl,
)
from bharat_terminal.kb.sync.rate_limiter import RateLimiter
from bharat_terminal.kb.sync.utils import (
    create_async_session,
    start_sync_log,
    complete_sync_log,
    compute_data_quality,
    invalidate_company_cache,
)

logger = logging.getLogger(__name__)

# Nifty50 symbols for tier-based processing
NIFTY50 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "BHARTIARTL", "SBIN", "BAJFINANCE", "ITC", "KOTAKBANK", "LT",
    "HCLTECH", "AXISBANK", "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN",
    "TATAMOTORS", "ULTRACEMCO", "NTPC", "WIPRO", "ADANIENT", "NESTLEIND",
    "JSWSTEEL", "BAJAJFINSV", "TATASTEEL", "POWERGRID", "M&M", "ONGC",
    "ADANIPORTS", "COALINDIA", "HDFCLIFE", "TECHM", "SBILIFE", "GRASIM",
    "DRREDDY", "INDUSINDBK", "CIPLA", "DIVISLAB", "BRITANNIA", "BPCL",
    "EICHERMOT", "APOLLOHOSP", "HINDALCO", "TATACONSUM", "HEROMOTOCO",
    "BAJAJ-AUTO", "SHRIRAMFIN", "LTIM",
]

# Backoff schedule for HTTP 429 (seconds)
BACKOFF_SCHEDULE = [5, 15, 45, 120]


# ── Phase 1: Company Master ────────────────────────────────────────────────

async def sync_company_master(session: AsyncSession) -> Dict:
    """
    Fetch all NSE-listed companies from equity master CSV and upsert.
    New companies get data_quality_score=0.1 (symbol+name stub).
    Existing companies get ISIN/listing_date updated if missing.
    """
    from bharat_terminal.kb.sync.nse_sync import fetch_nse_company_list

    log_id = await start_sync_log(session, "company_master", "nse")
    await session.commit()

    try:
        companies = await fetch_nse_company_list()
        logger.info(f"Fetched {len(companies)} companies from NSE equity master")

        added = 0
        updated = 0

        for c in companies:
            symbol = c["symbol"]
            result = await session.execute(
                select(Company).where(Company.symbol == symbol)
            )
            existing = result.scalar_one_or_none()

            if existing:
                # Update missing fields on existing stubs
                changed = False
                if not existing.isin and c.get("isin"):
                    existing.isin = c["isin"]
                    changed = True
                if not existing.listing_date and c.get("listing_date"):
                    try:
                        existing.listing_date = datetime.strptime(
                            c["listing_date"], "%d-%b-%Y"
                        ).date()
                        changed = True
                    except (ValueError, TypeError):
                        pass
                if changed:
                    updated += 1
            else:
                company = Company(
                    symbol=symbol,
                    isin=c.get("isin") or None,
                    exchange=c.get("exchange", "NSE"),
                    company_name=c.get("company_name", symbol),
                    aliases=[symbol],
                    is_active=True,
                    data_quality_score=0.1,
                )
                if c.get("listing_date"):
                    try:
                        company.listing_date = datetime.strptime(
                            c["listing_date"], "%d-%b-%Y"
                        ).date()
                    except (ValueError, TypeError):
                        pass
                session.add(company)
                added += 1

            if (added + updated) % 200 == 0 and (added + updated) > 0:
                await session.flush()

        await session.commit()

        await complete_sync_log(
            session, log_id, "success",
            records_processed=added + updated,
            metadata={"added": added, "updated": updated},
        )
        await session.commit()

        logger.info(f"Company master sync: {added} added, {updated} updated")
        return {"added": added, "updated": updated, "total_fetched": len(companies)}

    except Exception as e:
        logger.error(f"Company master sync failed: {e}")
        await complete_sync_log(session, log_id, "failed", error_message=str(e))
        await session.commit()
        raise


# ── Phase 2: Financial Results (NSE XBRL) ──────────────────────────────────

def _get_tier_symbols(tier: str) -> Optional[List[str]]:
    """Return symbol list for a tier, or None for 'all'."""
    if tier == "nifty50":
        return NIFTY50
    elif tier == "nifty500":
        return None  # Will be resolved from DB by mcap
    elif tier == "all":
        return None
    return NIFTY50


async def _resolve_symbols(
    session: AsyncSession,
    tier: str,
    full_refresh: bool,
    resume_after: Optional[str] = None,
) -> List[str]:
    """Resolve which symbols to process based on tier and refresh mode."""
    explicit = _get_tier_symbols(tier)

    if explicit:
        query = select(Company.symbol).where(Company.symbol.in_(explicit))
    elif tier == "nifty500":
        # Top 500 by market cap
        query = (
            select(Company.symbol)
            .where(Company.is_active == True)
            .order_by(Company.mcap_cr.desc().nullslast())
            .limit(500)
        )
    else:
        # All active companies
        query = select(Company.symbol).where(Company.is_active == True)

    if not full_refresh and not explicit:
        # Process companies that either have low data quality OR have no financial results yet.
        # This prevents silently skipping seed companies (e.g. Nifty 50 seeded with
        # quality_score=0.85-0.92 by seed_nifty50) that have never had XBRL filings fetched.
        fr_exists = select(FinancialResult.symbol).where(
            FinancialResult.symbol == Company.symbol
        ).limit(1)
        query = query.where(
            or_(
                Company.data_quality_score < 0.5,
                ~exists(fr_exists),
            )
        )

    result = await session.execute(query)
    symbols = [row[0] for row in result.all()]

    # Resumability: skip symbols we've already processed
    if resume_after:
        try:
            idx = symbols.index(resume_after)
            symbols = symbols[idx + 1:]
            logger.info(f"Resuming after {resume_after}, {len(symbols)} symbols remaining")
        except ValueError:
            pass

    return symbols


async def _upsert_nse_filing(session: AsyncSession, meta: Dict) -> Optional[int]:
    """Insert or update an nse_filings row. Returns the filing ID."""
    if not meta.get("from_date") or not meta.get("to_date"):
        return None

    stmt = pg_insert(NseFiling.__table__).values(
        symbol=meta["symbol"],
        isin=meta.get("isin"),
        from_date=meta["from_date"],
        to_date=meta["to_date"],
        period=meta["period"],
        relating_to=meta.get("relating_to"),
        financial_year=meta.get("financial_year"),
        consolidated=meta["consolidated"],
        audited=meta["audited"],
        ind_as=meta.get("ind_as"),
        format=meta.get("format"),
        xbrl_url=meta.get("xbrl_url"),
        detail_url=meta.get("detail_url"),
        seq_number=meta.get("seq_number"),
        filing_date=meta.get("filing_date"),
        broadcast_date=meta.get("broadcast_date"),
    ).on_conflict_do_update(
        constraint="uq_nse_filing",
        set_={
            "xbrl_url": meta.get("xbrl_url"),
            "detail_url": meta.get("detail_url"),
            "filing_date": meta.get("filing_date"),
        },
    ).returning(NseFiling.__table__.c.id)

    result = await session.execute(stmt)
    row = result.first()
    return row[0] if row else None


async def _upsert_financial_result(
    session: AsyncSession,
    symbol: str,
    filing_meta: Dict,
    parsed: Dict,
    nse_filing_id: Optional[int],
) -> bool:
    """Upsert a single financial_results row. Returns True if data was new/updated."""
    quarter = parsed.get("quarter", {})
    ytd = parsed.get("ytd", {})
    xbrl_meta = parsed.get("metadata", {})

    period_end = xbrl_meta.get("period_end") or filing_meta.get("to_date")
    period_start = xbrl_meta.get("period_start") or filing_meta.get("from_date")
    if not period_end or not period_start:
        return False

    is_consolidated = (
        filing_meta.get("consolidated", "") == "Consolidated"
        or "consolidated" in xbrl_meta.get("consolidated", "").lower()
    )
    is_audited = (
        filing_meta.get("audited", "") == "Audited"
        or "audited" in xbrl_meta.get("audited", "").lower()
    )

    values = {
        "symbol": symbol,
        "period_end": period_end,
        "period_start": period_start,
        "period_type": "quarterly",
        "consolidated": is_consolidated,
        "audited": is_audited,
        # P&L
        "revenue_cr": quarter.get("revenue_cr"),
        "other_income_cr": quarter.get("other_income_cr"),
        "total_income_cr": quarter.get("total_income_cr"),
        "cost_of_materials_cr": quarter.get("cost_of_materials_cr"),
        "purchases_stock_in_trade_cr": quarter.get("purchases_stock_in_trade_cr"),
        "inventory_changes_cr": quarter.get("inventory_changes_cr"),
        "employee_expense_cr": quarter.get("employee_expense_cr"),
        "finance_costs_cr": quarter.get("finance_costs_cr"),
        "depreciation_cr": quarter.get("depreciation_cr"),
        "other_expenses_cr": quarter.get("other_expenses_cr"),
        "total_expenses_cr": quarter.get("total_expenses_cr"),
        "pbt_before_exceptional_cr": quarter.get("pbt_before_exceptional_cr"),
        "exceptional_items_cr": quarter.get("exceptional_items_cr"),
        "pbt_cr": quarter.get("pbt_cr"),
        "tax_cr": quarter.get("tax_cr"),
        "pat_cr": quarter.get("pat_cr"),
        "pat_attributable_cr": quarter.get("pat_attributable_cr"),
        "other_comprehensive_income_cr": quarter.get("other_comprehensive_income_cr"),
        # EPS
        "eps_basic": quarter.get("eps_basic"),
        "eps_diluted": quarter.get("eps_diluted"),
        "face_value": quarter.get("face_value"),
        "paid_up_capital_cr": quarter.get("paid_up_capital_cr"),
        # Ratios
        "debt_equity_ratio": quarter.get("debt_equity_ratio"),
        "interest_coverage_ratio": quarter.get("interest_coverage_ratio"),
        # Derived
        "ebitda_cr": quarter.get("ebitda_cr"),
        "ebitda_margin_pct": quarter.get("ebitda_margin_pct"),
        # Segment
        "segment_data": parsed.get("segment_data") or None,
        # YTD
        "ytd_revenue_cr": ytd.get("revenue_cr"),
        "ytd_pat_cr": ytd.get("pat_cr"),
        "ytd_eps_basic": ytd.get("eps_basic"),
        # Provenance
        "source": "nse_xbrl",
        "nse_filing_id": nse_filing_id,
        "raw_xbrl_facts": parsed.get("raw_facts"),
    }

    stmt = pg_insert(FinancialResult.__table__).values(**values).on_conflict_do_update(
        constraint="uq_financial_result",
        set_={
            k: v for k, v in values.items()
            if k not in ("symbol", "period_end", "period_type", "consolidated", "created_at")
        },
    )
    await session.execute(stmt)
    return True


async def _compute_ttm_and_update_company(
    session: AsyncSession,
    symbol: str,
) -> None:
    """
    Compute TTM (trailing twelve months) aggregates from the 4 most recent
    quarterly results and update the companies table.
    """
    # Fetch last 4 consolidated quarterly results
    result = await session.execute(
        select(FinancialResult)
        .where(
            FinancialResult.symbol == symbol,
            FinancialResult.period_type == "quarterly",
            FinancialResult.consolidated == True,
        )
        .order_by(FinancialResult.period_end.desc())
        .limit(4)
    )
    quarters = result.scalars().all()

    if not quarters:
        return

    # Get the company
    result = await session.execute(select(Company).where(Company.symbol == symbol))
    company = result.scalar_one_or_none()
    if not company:
        return

    # Sum TTM revenue and PAT from available quarters
    revenues = [q.revenue_cr for q in quarters if q.revenue_cr is not None]
    pats = [q.pat_cr for q in quarters if q.pat_cr is not None]

    if revenues:
        company.revenue_ttm_cr = round(sum(revenues), 2)
    if pats:
        company.pat_cr = round(sum(pats), 2)

    # Use most recent quarter for margin, EPS, ratios
    latest = quarters[0]
    if latest.ebitda_margin_pct is not None:
        company.ebitda_margin_pct = latest.ebitda_margin_pct
    if latest.eps_basic is not None:
        # TTM EPS = sum of 4 quarters' EPS
        eps_list = [q.eps_basic for q in quarters if q.eps_basic is not None]
        if eps_list:
            company.eps_ttm = round(sum(eps_list), 2)
    if latest.debt_equity_ratio is not None:
        company.interest_coverage = latest.interest_coverage_ratio

    company.financials_updated_at = datetime.now(timezone.utc)

    # Compute data quality
    has_quarterly = len(quarters) >= 4
    has_rels_result = await session.execute(
        select(sa_func.count()).select_from(Company.__table__)
        .join(
            Company.relationships_as_source.property.mapper.class_.__table__,
            Company.__table__.c.symbol == Company.relationships_as_source.property.mapper.class_.__table__.c.source_symbol,
            isouter=True,
        )
        .where(Company.symbol == symbol)
    )
    # Simpler approach: just check if relationships exist
    from bharat_terminal.kb.models import CompanyRelationship
    rel_count_result = await session.execute(
        select(sa_func.count())
        .where(CompanyRelationship.source_symbol == symbol)
    )
    has_rels = (rel_count_result.scalar() or 0) > 0

    company.data_quality_score = compute_data_quality(
        company, has_quarterly_data=has_quarterly, has_relationships=has_rels
    )


async def _process_symbol_financials(
    symbol: str,
    client,
    limiter: RateLimiter,
    session: AsyncSession,
    skipped_symbols: List[str],
) -> Dict:
    """Process all financial filings for a single symbol. Returns stats dict."""
    stats = {"symbol": symbol, "filings_found": 0, "parsed": 0, "failed": 0, "skipped": 0}

    # Fetch filing index from NSE (single attempt — skip on empty, don't waste time retrying)
    raw_filings = await fetch_filing_index(symbol, client, limiter)

    if not raw_filings:
        skipped_symbols.append(symbol)
        logger.debug(f"No filings for {symbol}, skipping")
        return stats

    # Parse metadata and filter to consolidated quarterly with XBRL
    parsed_metas = [parse_filing_metadata(r) for r in raw_filings]
    filings = filter_consolidated_quarterly(parsed_metas)
    stats["filings_found"] = len(filings)

    for filing_meta in filings:
        if not filing_meta.get("xbrl_url"):
            stats["skipped"] += 1
            continue

        # Check if already parsed
        existing = await session.execute(
            select(NseFiling).where(
                NseFiling.symbol == symbol,
                NseFiling.from_date == filing_meta["from_date"],
                NseFiling.to_date == filing_meta["to_date"],
                NseFiling.consolidated == filing_meta["consolidated"],
                NseFiling.xbrl_parsed == True,
            )
        )
        if existing.scalar_one_or_none():
            stats["skipped"] += 1
            continue

        # Upsert filing metadata
        nse_filing_id = await _upsert_nse_filing(session, filing_meta)

        # Download and parse XBRL
        parsed = await fetch_and_parse_xbrl(filing_meta, client, limiter)
        if not parsed:
            stats["failed"] += 1
            continue

        # Upsert financial result
        ok = await _upsert_financial_result(
            session, symbol, filing_meta, parsed, nse_filing_id
        )
        if ok:
            stats["parsed"] += 1
            # Mark filing as parsed
            if nse_filing_id:
                await session.execute(
                    NseFiling.__table__.update()
                    .where(NseFiling.__table__.c.id == nse_filing_id)
                    .values(xbrl_downloaded=True, xbrl_parsed=True)
                )
        else:
            stats["failed"] += 1

    # Compute TTM and update company after all quarters are in
    if stats["parsed"] > 0:
        await _compute_ttm_and_update_company(session, symbol)
        await invalidate_company_cache(symbol)

    return stats


async def sync_financials(
    tier: str = "nifty50",
    full_refresh: bool = False,
    resume: bool = False,
) -> Dict:
    """
    Fetch NSE XBRL filings for companies in the given tier,
    parse them, and store quarterly P&L in financial_results.

    Tiers:
      nifty50  — ~50 companies
      nifty500 — top 500 by market cap
      all      — every active company (~2500)
    """
    SessionFactory = create_async_session()

    async with SessionFactory() as session:
        log_id = await start_sync_log(session, "financials", "nse_xbrl")
        await session.commit()

        try:
            # Resolve resume point
            resume_after = None
            if resume:
                prev = await session.execute(
                    select(SyncLog)
                    .where(
                        SyncLog.sync_type == "financials",
                        SyncLog.status == "failed",
                    )
                    .order_by(SyncLog.started_at.desc())
                    .limit(1)
                )
                prev_log = prev.scalar_one_or_none()
                if prev_log and prev_log.metadata_json:
                    resume_after = prev_log.metadata_json.get("last_symbol_processed")

            symbols = await _resolve_symbols(session, tier, full_refresh, resume_after)
            logger.info(f"Financial sync: {len(symbols)} symbols to process (tier={tier})")

            if not symbols:
                await complete_sync_log(session, log_id, "success", records_processed=0)
                await session.commit()
                return {"processed": 0, "total_parsed": 0, "total_failed": 0}

            # Create shared NSE client and rate limiter
            client = await create_nse_client()
            limiter = RateLimiter(rate=3.0)  # 3 req/sec for NSE

            total_parsed = 0
            total_failed = 0
            last_symbol = None
            skipped_symbols: List[str] = []

            try:
                for i, symbol in enumerate(symbols):
                    # Refresh NSE cookies every 50 symbols to prevent session expiry
                    if i > 0 and i % 50 == 0:
                        logger.info("Refreshing NSE session cookies")
                        await refresh_nse_cookies(client)

                    try:
                        stats = await _process_symbol_financials(
                            symbol, client, limiter, session, skipped_symbols
                        )
                        total_parsed += stats["parsed"]
                        total_failed += stats["failed"]
                    except Exception as e:
                        logger.warning(f"Symbol {symbol} failed: {e}")
                        total_failed += 1
                        # Rollback the failed transaction so session stays usable
                        await session.rollback()

                    last_symbol = symbol

                    # Commit in batches of 10
                    if (i + 1) % 10 == 0:
                        await session.commit()

                    # Progress logging
                    if (i + 1) % 25 == 0 or (i + 1) == len(symbols):
                        logger.info(
                            f"Progress: {i+1}/{len(symbols)} symbols | "
                            f"parsed={total_parsed} failed={total_failed} "
                            f"skipped_empty={len(skipped_symbols)}"
                        )

                await session.commit()
            finally:
                await client.aclose()

            if skipped_symbols:
                logger.info(
                    f"Skipped {len(skipped_symbols)} symbols with no filings: "
                    f"{skipped_symbols[:20]}{'...' if len(skipped_symbols) > 20 else ''}"
                )

            await complete_sync_log(
                session, log_id, "success",
                records_processed=total_parsed,
                records_failed=total_failed,
                metadata={
                    "tier": tier,
                    "symbols_processed": len(symbols),
                    "last_symbol_processed": last_symbol,
                    "skipped_empty_count": len(skipped_symbols),
                    "skipped_empty_symbols": skipped_symbols,
                },
            )
            await session.commit()

            summary = {
                "processed": len(symbols),
                "total_parsed": total_parsed,
                "total_failed": total_failed,
                "skipped_empty": len(skipped_symbols),
            }
            logger.info(f"Financial sync complete: {summary}")
            return summary

        except Exception as e:
            logger.error(f"Financial sync failed: {e}")
            try:
                await session.rollback()
                await complete_sync_log(
                    session, log_id, "failed",
                    records_processed=total_parsed,
                    records_failed=total_failed,
                    error_message=str(e)[:500],
                    metadata={"last_symbol_processed": last_symbol},
                )
                await session.commit()
            except Exception:
                pass
            raise


# ── Single-Symbol Sync (event-driven) ─────────────────────────────────────

async def sync_symbol(symbol: str) -> Dict:
    """
    Sync financial data for a single company.
    Used by the event-driven monitor after a result announcement.
    """
    from bharat_terminal.kb.sync.nse_financial_results import (
        create_nse_client, refresh_nse_cookies,
    )

    SessionFactory = create_async_session()
    limiter = RateLimiter(rate=3.0)

    async with SessionFactory() as session:
        # Verify symbol exists
        comp = await session.execute(
            select(Company).where(Company.symbol == symbol)
        )
        if not comp.scalar_one_or_none():
            logger.warning(f"sync_symbol: unknown symbol {symbol}")
            return {"symbol": symbol, "status": "unknown_symbol"}

        client = await create_nse_client()
        try:
            skipped: List[str] = []

            try:
                stats = await _process_symbol_financials(
                    symbol, client, limiter, session, skipped
                )
                await session.commit()

                if stats["parsed"] > 0:
                    # Also refresh DCF for this company
                    try:
                        from bharat_terminal.kb.dcf import DCFInputs, compute_dcf
                        company = (await session.execute(
                            select(Company).where(Company.symbol == symbol)
                        )).scalar_one_or_none()
                        if company and company.revenue_ttm_cr:
                            inputs = DCFInputs(
                                revenue_ttm_cr=company.revenue_ttm_cr or 0,
                                pat_ttm_cr=company.pat_ttm_cr or 0,
                                market_cap_cr=company.market_cap_cr or 0,
                                sector=company.sector or "Unknown",
                            )
                            dcf = compute_dcf(inputs)
                            company.dcf_intrinsic_pe = dcf.get("intrinsic_pe")
                            company.dcf_target_price = dcf.get("target_price")
                            company.dcf_margin_of_safety = dcf.get("margin_of_safety_pct")
                            company.dcf_updated_at = datetime.now(timezone.utc)
                            await session.commit()
                            stats["dcf_refreshed"] = True
                    except Exception as e:
                        logger.warning(f"DCF refresh failed for {symbol}: {e}")

                logger.info(f"sync_symbol({symbol}): {stats}")
                return stats

            except Exception as e:
                logger.error(f"sync_symbol({symbol}) failed: {e}")
                await session.rollback()
                return {"symbol": symbol, "status": "error", "error": str(e)}
        finally:
            await client.aclose()


# ── Phase 3: DCF Refresh ───────────────────────────────────────────────────

async def sync_dcf_all() -> Dict:
    """Recompute DCF for all companies that have revenue data."""
    from bharat_terminal.kb.dcf import DCFInputs, compute_dcf

    SessionFactory = create_async_session()

    async with SessionFactory() as session:
        log_id = await start_sync_log(session, "dcf_refresh", "computed")
        await session.commit()

        result = await session.execute(
            select(Company).where(
                Company.revenue_ttm_cr.isnot(None),
                Company.is_active == True,
            )
        )
        companies = result.scalars().all()
        logger.info(f"DCF refresh: {len(companies)} companies with revenue data")

        ok = 0
        failed = 0

        for company in companies:
            try:
                if not company.revenue_ttm_cr:
                    continue

                # Derive shares outstanding
                if (company.pe_ratio and company.eps_ttm
                        and company.pe_ratio > 0 and company.eps_ttm > 0):
                    price = company.pe_ratio * company.eps_ttm
                    shares_cr = company.mcap_cr / price if company.mcap_cr else 100.0
                else:
                    shares_cr = 100.0

                inputs = DCFInputs(
                    symbol=company.symbol,
                    revenue_ttm_cr=company.revenue_ttm_cr,
                    ebitda_margin_pct=company.ebitda_margin_pct or 15.0,
                    net_debt_cr=company.net_debt_cr or 0.0,
                    shares_outstanding_cr=shares_cr,
                    equity_value_cr=company.mcap_cr,
                    source="nse_xbrl" if company.data_quality_score > 0.5 else "llm_inferred",
                    confidence=company.data_quality_score,
                )

                dcf_result = compute_dcf(inputs)

                company.wacc_pct = dcf_result.wacc_pct
                company.terminal_growth_pct = dcf_result.terminal_growth_pct
                company.year_projections = [
                    {"year": p.year, "revenue": p.revenue, "ebitda": p.ebitda,
                     "pat": p.pat, "eps": p.eps, "fcf": p.fcf}
                    for p in dcf_result.year_projections
                ]
                company.fair_value_per_share = dcf_result.fair_value_per_share
                company.margin_of_safety_pct = dcf_result.margin_of_safety_pct
                company.dcf_bull_value = dcf_result.bull_value
                company.dcf_bear_value = dcf_result.bear_value
                company.dcf_updated_at = datetime.now(timezone.utc)
                company.dcf_source = inputs.source
                company.dcf_confidence = inputs.confidence

                ok += 1
            except Exception as e:
                logger.warning(f"DCF failed for {company.symbol}: {e}")
                failed += 1

        await session.commit()

        await complete_sync_log(
            session, log_id, "success",
            records_processed=ok, records_failed=failed,
        )
        await session.commit()

        logger.info(f"DCF refresh complete: {ok} ok, {failed} failed")
        return {"ok": ok, "failed": failed}


# ── Phase 4: Price Sync ────────────────────────────────────────────────────

async def sync_prices(days: int = 30) -> Dict:
    """
    Backfill daily OHLCV from NSE bhavcopy.
    Downloads one bhavcopy per trading day (bulk — all symbols at once).
    Skips dates already loaded.
    """
    from bharat_terminal.kb.sync.prices_sync import fetch_bhavcopy, last_trading_day

    SessionFactory = create_async_session()

    async with SessionFactory() as session:
        log_id = await start_sync_log(session, "prices", "nse_bhavcopy")
        await session.commit()

        limiter = RateLimiter(rate=1.0)  # 1 req/sec for bhavcopy downloads

        total_records = 0
        days_loaded = 0
        days_skipped = 0
        failed_days = 0

        try:
            # Load known symbols to filter bhavcopy (avoids FK violations)
            known_result = await session.execute(
                select(Company.symbol)
            )
            known_symbols = {row[0] for row in known_result.fetchall()}
            logger.info(f"Loaded {len(known_symbols)} known symbols for price filtering")

            today = last_trading_day()

            for day_offset in range(days):
                target_date = today - timedelta(days=day_offset)

                # Skip weekends
                if target_date.weekday() >= 5:
                    continue

                # Check if we already have data for this date
                existing = await session.execute(
                    select(sa_func.count())
                    .select_from(PricePoint.__table__)
                    .where(PricePoint.date == target_date)
                )
                count = existing.scalar() or 0
                if count > 100:
                    days_skipped += 1
                    continue

                async with limiter:
                    try:
                        prices = await fetch_bhavcopy(target_date)
                    except Exception as e:
                        logger.warning(f"Bhavcopy failed for {target_date}: {e}")
                        failed_days += 1
                        continue

                if not prices:
                    continue

                # Filter to only symbols present in companies table
                prices = [p for p in prices if p["symbol"] in known_symbols]
                if not prices:
                    continue

                # Bulk upsert using INSERT ON CONFLICT DO NOTHING
                try:
                    for price in prices:
                        stmt = pg_insert(PricePoint.__table__).values(
                            symbol=price["symbol"],
                            date=price["date"],
                            open=price.get("open"),
                            high=price.get("high"),
                            low=price.get("low"),
                            close=price["close"],
                            volume=price.get("volume"),
                        ).on_conflict_do_nothing(constraint="uq_price_point")
                        await session.execute(stmt)

                    total_records += len(prices)
                    days_loaded += 1
                except Exception as e:
                    logger.warning(f"Price insert failed for {target_date}: {e}")
                    await session.rollback()
                    failed_days += 1
                    continue

                if days_loaded % 5 == 0:
                    await session.commit()
                    logger.info(f"Prices: {days_loaded} days loaded, {total_records} records")

            await session.commit()

            await complete_sync_log(
                session, log_id, "success",
                records_processed=total_records,
                metadata={"days_loaded": days_loaded, "days_skipped": days_skipped,
                          "failed_days": failed_days},
            )
            await session.commit()

            summary = {
                "days_loaded": days_loaded,
                "days_skipped": days_skipped,
                "failed_days": failed_days,
                "total_records": total_records,
            }
            logger.info(f"Price sync complete: {summary}")
            return summary

        except Exception as e:
            logger.error(f"Price sync failed: {e}")
            await session.rollback()
            await complete_sync_log(session, log_id, "failed", error_message=str(e))
            await session.commit()
            raise


# ── Phase 5: Full Pipeline ─────────────────────────────────────────────────

async def sync_all(
    tier: str = "nifty50",
    days: int = 30,
    full_refresh: bool = False,
) -> Dict:
    """
    Run all sync phases sequentially:
      1. Company master (always)
      2. Financials (by tier)
      3. DCF refresh
      4. Price sync

    Returns summary dict with results from each phase.
    """
    logger.info(f"Starting full sync pipeline: tier={tier}, days={days}")
    results = {}

    # Phase 1: Company master
    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        results["company_master"] = await sync_company_master(session)

    # Phase 2: Financials
    results["financials"] = await sync_financials(tier=tier, full_refresh=full_refresh)

    # Phase 3: DCF
    results["dcf"] = await sync_dcf_all()

    # Phase 4: Prices
    results["prices"] = await sync_prices(days=days)

    logger.info(f"Full sync pipeline complete: {results}")
    return results

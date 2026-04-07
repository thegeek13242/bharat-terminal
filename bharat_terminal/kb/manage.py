#!/usr/bin/env python
"""
Management CLI for Bharat Terminal Knowledge Base.
Usage:
  python -m bharat_terminal.kb.manage refresh-dcf --symbol RELIANCE
  python -m bharat_terminal.kb.manage seed-reliance   (for testing)
  python -m bharat_terminal.kb.manage create-tables
"""
import asyncio
import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select

from bharat_terminal.kb.models import Base, Company, CompanyRelationship
from bharat_terminal.kb.dcf import DCFInputs, compute_dcf

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://bharat:bharat@localhost:5432/bharat_terminal")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def refresh_dcf(symbol: str):
    """Recompute DCF model for a given symbol."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Company).where(Company.symbol == symbol.upper()))
        company = result.scalar_one_or_none()

        if not company:
            logger.error(f"Company {symbol} not found in database")
            return

        logger.info(f"Refreshing DCF for {company.symbol} ({company.company_name})")

        if not company.revenue_ttm_cr:
            logger.error(f"No revenue data for {symbol}, cannot compute DCF")
            return

        # Derive shares outstanding from market cap / price (price = EPS * PE)
        if company.pe_ratio and company.eps_ttm and company.pe_ratio > 0 and company.eps_ttm > 0:
            price_per_share = company.pe_ratio * company.eps_ttm
            shares_cr = company.mcap_cr / price_per_share if company.mcap_cr else 100.0
        else:
            shares_cr = 100.0

        inputs = DCFInputs(
            symbol=company.symbol,
            revenue_ttm_cr=company.revenue_ttm_cr,
            ebitda_margin_pct=company.ebitda_margin_pct or 15.0,
            net_debt_cr=company.net_debt_cr or 0.0,
            shares_outstanding_cr=shares_cr,
            equity_value_cr=company.mcap_cr,
            source="screener_api" if company.data_quality_score > 0.5 else "llm_inferred",
            confidence=company.data_quality_score,
        )

        dcf_result = compute_dcf(inputs)

        company.wacc_pct = dcf_result.wacc_pct
        company.terminal_growth_pct = dcf_result.terminal_growth_pct
        company.year_projections = [
            {
                "year": p.year,
                "revenue": p.revenue,
                "ebitda": p.ebitda,
                "pat": p.pat,
                "eps": p.eps,
                "fcf": p.fcf,
            }
            for p in dcf_result.year_projections
        ]
        company.fair_value_per_share = dcf_result.fair_value_per_share
        company.margin_of_safety_pct = dcf_result.margin_of_safety_pct
        company.dcf_bull_value = dcf_result.bull_value
        company.dcf_bear_value = dcf_result.bear_value
        company.dcf_updated_at = datetime.now(timezone.utc)
        company.dcf_source = inputs.source
        company.dcf_confidence = inputs.confidence

        await session.commit()

        from bharat_terminal.kb.dcf import print_sensitivity_table
        print_sensitivity_table(dcf_result)
        logger.info(f"DCF refreshed for {symbol}: fair value Rs.{dcf_result.fair_value_per_share}")


async def seed_reliance():
    """Seed RELIANCE.NS with sample data for testing (includes >=5 relationships)."""
    async with AsyncSessionLocal() as session:
        # Upsert RELIANCE
        result = await session.execute(select(Company).where(Company.symbol == "RELIANCE"))
        company = result.scalar_one_or_none()

        if not company:
            company = Company(symbol="RELIANCE")
            session.add(company)

        company.isin = "INE002A01018"
        company.exchange = "BOTH"
        company.company_name = "Reliance Industries Limited"
        company.aliases = ["RIL", "Reliance", "Reliance Industries"]
        company.sector_nse = "OIL_GAS"
        company.industry_nse = "Refineries"
        company.bse_group = "A"
        company.mcap_cr = 1700000.0
        company.description_200w = (
            "Reliance Industries Limited is India's largest private sector company, "
            "with diversified operations across energy (refining, petrochemicals, oil & gas), "
            "retail (Reliance Retail), telecom (Jio), and media & entertainment. "
            "The company is the largest refiner in India with a 68 MMTPA refining capacity "
            "at Jamnagar, Gujarat. Jio Platforms is India's largest telecom operator by subscribers."
        )
        company.revenue_segments = [
            {"name": "O2C (Oil-to-Chemicals)", "pct_of_revenue": 55.0, "yoy_growth": 8.0},
            {"name": "Retail", "pct_of_revenue": 20.0, "yoy_growth": 18.0},
            {"name": "Digital Services (Jio)", "pct_of_revenue": 15.0, "yoy_growth": 12.0},
            {"name": "Oil & Gas E&P", "pct_of_revenue": 5.0, "yoy_growth": 5.0},
            {"name": "Media & Others", "pct_of_revenue": 5.0, "yoy_growth": 25.0},
        ]
        company.geography_split = {"India": 78.0, "USA": 8.0, "Europe": 7.0, "Others": 7.0}
        company.moat_classification = "COST_ADVANTAGE"
        company.revenue_ttm_cr = 900000.0
        company.ebitda_margin_pct = 17.5
        company.pat_cr = 78000.0
        company.eps_ttm = 115.0
        company.pe_ratio = 24.5
        company.pb_ratio = 2.2
        company.roe_pct = 9.8
        company.net_debt_cr = 120000.0
        company.interest_coverage = 8.5
        company.fcf_yield_pct = 3.2
        company.median_target_price = 3200.0
        company.buy_pct = 65.0
        company.hold_pct = 25.0
        company.sell_pct = 10.0
        company.num_analysts = 32
        company.eps_fy_curr = 120.0
        company.eps_fy_next = 138.0
        company.is_active = True
        company.data_quality_score = 0.9
        company.financials_updated_at = datetime.now(timezone.utc)

        await session.flush()

        # Add >=5 relationships
        relationships = [
            ("RELIANCE", "HPCL", "Hindustan Petroleum Corporation", "COMPETITOR", 0.7),
            ("RELIANCE", "BPCL", "Bharat Petroleum Corporation", "COMPETITOR", 0.7),
            ("RELIANCE", "ONGC", "Oil and Natural Gas Corporation", "CUSTOMER", 0.6),
            ("RELIANCE", "JIOFINANCE", "Jio Financial Services", "SUBSIDIARY", 0.9),
            ("RELIANCE", "NETWORK18", "Network18 Media & Investments", "SUBSIDIARY", 0.85),
            ("RELIANCE", "TITAN", "Titan Company Limited", "COMPETITOR", 0.3),
            ("RELIANCE", "INDIGO", "IndiGo Airlines (InterGlobe Aviation)", "CUSTOMER", 0.4),
        ]

        for src, tgt, tgt_name, rel_type, weight in relationships:
            rel_result = await session.execute(
                select(CompanyRelationship).where(
                    CompanyRelationship.source_symbol == src,
                    CompanyRelationship.target_symbol == tgt,
                    CompanyRelationship.relationship_type == rel_type,
                )
            )
            rel = rel_result.scalar_one_or_none()
            if not rel:
                rel = CompanyRelationship(
                    source_symbol=src,
                    target_symbol=tgt,
                    target_name=tgt_name,
                    relationship_type=rel_type,
                    weight=weight,
                    source_of_truth="manual",
                    confidence=0.95,
                )
                session.add(rel)

        await session.commit()
        logger.info("RELIANCE seeded with 7 relationships")


async def seed_nifty50():
    """Seed major NIFTY 50 companies into the knowledge base."""
    COMPANIES = [
        # symbol, isin, exchange, name, sector, industry, mcap_cr, rev_cr, ebitda_pct, pat_cr, eps, pe, pb, roe, net_debt_cr, dq
        ("SBIN",       "INE062A01020", "BOTH", "State Bank of India",              "BANKING",     "PSU Banks",           700000, 450000, 28.0, 62000, 69.0,  10.5, 1.4, 18.0,  -200000, 0.85),
        ("HDFCBANK",   "INE040A01034", "BOTH", "HDFC Bank Limited",                "BANKING",     "Private Banks",       1100000, 330000, 40.0, 64000, 83.0,  18.0, 2.5, 17.0,  -500000, 0.90),
        ("ICICIBANK",  "INE090A01021", "BOTH", "ICICI Bank Limited",               "BANKING",     "Private Banks",       850000,  250000, 42.0, 48000, 68.0,  17.5, 2.8, 18.5,  -300000, 0.90),
        ("AXISBANK",   "INE238A01034", "BOTH", "Axis Bank Limited",                "BANKING",     "Private Banks",       360000,  135000, 35.0, 26000, 85.0,  14.0, 1.8, 16.0,  -150000, 0.85),
        ("KOTAKBANK",  "INE237A01028", "BOTH", "Kotak Mahindra Bank Limited",      "BANKING",     "Private Banks",       380000,  95000,  45.0, 14000, 71.0,  27.0, 3.2, 14.0,  -80000,  0.85),
        ("TCS",        "INE467B01029", "BOTH", "Tata Consultancy Services",        "IT",          "IT Services",         1400000, 240000, 24.5, 46000, 126.0, 30.0, 12.0, 52.0, -80000,  0.92),
        ("INFY",       "INE009A01021", "BOTH", "Infosys Limited",                  "IT",          "IT Services",         600000,  155000, 21.0, 26000, 62.0,  23.0, 6.0, 32.0,  -40000,  0.92),
        ("WIPRO",      "INE075A01022", "BOTH", "Wipro Limited",                    "IT",          "IT Services",         240000,  89000,  17.5, 11000, 21.0,  22.0, 3.5, 17.0,  -20000,  0.85),
        ("HCLTECH",    "INE860A01027", "BOTH", "HCL Technologies Limited",         "IT",          "IT Services",         420000,  108000, 19.0, 16000, 60.0,  26.0, 7.0, 25.0,  -15000,  0.85),
        ("TECHM",      "INE669C01036", "BOTH", "Tech Mahindra Limited",            "IT",          "IT Services",         120000,  52000,  12.5, 3000,  30.0,  40.0, 3.0, 8.0,   -5000,   0.80),
        ("HINDUNILVR", "INE030A01027", "BOTH", "Hindustan Unilever Limited",       "FMCG",        "Diversified FMCG",   540000,  60000,  23.0, 10500, 44.5,  51.0, 11.0, 24.0,  -8000,   0.90),
        ("ITC",        "INE154A01025", "BOTH", "ITC Limited",                      "FMCG",        "Cigarettes & FMCG",  540000,  72000,  35.0, 20000, 16.0,  27.0, 7.0, 30.0,  -30000,  0.88),
        ("NESTLEIND",  "INE239A01016", "NSE",  "Nestle India Limited",             "FMCG",        "Packaged Foods",     230000,  22000,  24.0, 3600,  373.0, 64.0, 80.0, 120.0, -5000,   0.85),
        ("TATAMOTORS", "INE155A01022", "BOTH", "Tata Motors Limited",              "AUTO",        "Automobiles",        280000,  450000, 12.0, 24000, 62.0,  11.0, 2.4, 25.0,  120000,  0.82),
        ("MARUTI",     "INE585B01010", "BOTH", "Maruti Suzuki India Limited",      "AUTO",        "Automobiles",        380000,  140000, 12.5, 13500, 449.0, 28.0, 5.5, 20.0,  -20000,  0.88),
        ("M&M",        "INE101A01026", "BOTH", "Mahindra and Mahindra Limited",    "AUTO",        "Automobiles",        320000,  130000, 14.0, 12000, 97.0,  27.0, 5.0, 20.0,  40000,   0.85),
        ("BAJFINANCE",  "INE296A01024", "BOTH", "Bajaj Finance Limited",           "NBFC",        "Consumer Finance",  480000,  52000,  55.0, 16500, 267.0, 29.0, 6.5, 26.0,  -280000, 0.90),
        ("BAJAJFINSV",  "INE918I01026", "BOTH", "Bajaj Finserv Limited",           "NBFC",        "Financial Services", 230000,  80000,  22.0, 8000,  50.0,  29.0, 3.5, 14.0,  -50000,  0.85),
        ("ONGC",       "INE213A01029", "BOTH", "Oil and Natural Gas Corporation",  "OIL_GAS",     "Oil Exploration",   290000,  650000, 22.0, 38000, 30.0,  8.0,  1.1, 15.0,  60000,   0.85),
        ("BPCL",       "INE029A01011", "BOTH", "Bharat Petroleum Corporation",     "OIL_GAS",     "Refineries",        130000,  480000, 4.5,  9000,  42.0,  14.5, 1.5, 11.0,  30000,   0.82),
        ("IOC",        "INE242A01010", "BOTH", "Indian Oil Corporation",           "OIL_GAS",     "Refineries",        180000,  900000, 5.0,  15000, 11.0,  12.0, 1.2, 11.0,  80000,   0.82),
        ("COALINDIA",  "INE522F01014", "BOTH", "Coal India Limited",               "MINING",      "Coal",              240000,  140000, 28.0, 34000, 55.0,  7.0,  3.0, 50.0,  -60000,  0.85),
        ("NTPC",       "INE733E01010", "BOTH", "NTPC Limited",                     "POWER",       "Power Generation",  330000,  180000, 30.0, 20000, 22.0,  16.0, 1.8, 12.0,  200000,  0.85),
        ("POWERGRID",  "INE752E01010", "BOTH", "Power Grid Corporation of India",  "POWER",       "Power Transmission", 270000, 46000,  68.0, 15000, 29.0,  18.0, 3.2, 19.0,  120000,  0.85),
        ("SUNPHARMA",  "INE044A01036", "BOTH", "Sun Pharmaceutical Industries",    "PHARMA",      "Pharmaceuticals",   380000,  50000,  26.0, 11000, 46.0,  35.0, 5.5, 18.0,  -10000,  0.88),
        ("DRREDDY",    "INE089A01023", "BOTH", "Dr. Reddy's Laboratories",         "PHARMA",      "Pharmaceuticals",   100000,  28000,  22.0, 5800,  348.0, 17.0, 3.5, 22.0,  -5000,   0.85),
        ("CIPLA",      "INE059A01026", "BOTH", "Cipla Limited",                    "PHARMA",      "Pharmaceuticals",   100000,  25000,  22.0, 4500,  56.0,  22.0, 3.8, 18.0,  -8000,   0.85),
        ("DIVISLAB",   "INE361B01024", "NSE",  "Divi's Laboratories Limited",      "PHARMA",      "API/Generics",      75000,   8500,   38.0, 2200,  83.0,  34.0, 7.0, 22.0,  -5000,   0.82),
        ("LTIM",       "INE214T01019", "BOTH", "LTIMindtree Limited",              "IT",          "IT Services",       130000,  36000,  18.0, 5500,  186.0, 24.0, 6.0, 27.0,  -5000,   0.82),
        ("ADANIPORTS", "INE742F01042", "BOTH", "Adani Ports and SEZ Limited",      "INFRA",       "Ports & Logistics", 270000,  27000,  55.0, 9000,  42.0,  30.0, 4.5, 16.0,  50000,   0.80),
        ("ADANIENT",   "INE423A01024", "BOTH", "Adani Enterprises Limited",        "INFRA",       "Diversified",       300000,  85000,  12.0, 3000,  27.0,  100.0, 8.0, 9.0,  80000,   0.78),
        ("TITAN",      "INE280A01028", "BOTH", "Titan Company Limited",            "CONSUMER",    "Jewellery & Watches", 310000, 53000, 10.5, 4800,  54.0,  65.0, 18.0, 32.0, -12000,  0.88),
        ("BHARTIARTL", "INE397D01024", "BOTH", "Bharti Airtel Limited",            "TELECOM",     "Telecom",           870000,  150000, 45.0, 15000, 25.0,  58.0, 8.0, 16.0,  200000,  0.88),
        ("JIOFINANCE", "INE758T01015", "BOTH", "Jio Financial Services Limited",   "NBFC",        "Financial Services", 160000,  4000,  30.0, 1500,  24.0,  107.0, 2.0, 2.0,  -10000,  0.72),
        ("LT",         "INE018A01030", "BOTH", "Larsen and Toubro Limited",        "INFRA",       "Engineering & Construction", 500000, 230000, 12.0, 15000, 107.0, 33.0, 5.0, 16.0, 50000, 0.88),
        ("ULTRACEMCO",  "INE481G01011", "BOTH", "UltraTech Cement Limited",        "CEMENT",      "Cement",            290000,  67000,  20.0, 8000,  277.0, 36.0, 5.5, 16.0,  30000,   0.85),
        ("SHREECEM",   "INE070A01015", "BOTH", "Shree Cement Limited",             "CEMENT",      "Cement",            80000,   19000,  22.0, 2500,  692.0, 32.0, 4.5, 15.0,  10000,   0.82),
        ("ASIANPAINT", "INE021A01026", "BOTH", "Asian Paints Limited",             "CHEMICALS",   "Paints",            240000,  35000,  19.0, 5500,  57.0,  44.0, 12.0, 32.0,  -8000,   0.90),
        ("GRASIM",     "INE047A01021", "BOTH", "Grasim Industries Limited",        "CEMENT",      "Diversified",       155000,  130000, 15.0, 9000,  147.0, 17.0, 1.9, 12.0,  40000,   0.82),
        ("NESTLEIND",  "INE239A01016", "NSE",  "Nestle India Limited",             "FMCG",        "Packaged Foods",    230000,  22000,  24.0, 3600,  373.0, 64.0, 80.0, 120.0, -5000,   0.85),
        ("INDUSINDBK", "INE095A01012", "BOTH", "IndusInd Bank Limited",            "BANKING",     "Private Banks",     90000,   55000,  35.0, 8000,  103.0, 11.0, 1.2, 11.0,  -80000,  0.80),
        ("BRITANNIA",  "INE216A01030", "BOTH", "Britannia Industries Limited",     "FMCG",        "Food Products",     115000,  17000,  17.0, 1900,  79.0,  61.0, 25.0, 50.0,  -5000,   0.85),
        ("BAJAJ-AUTO", "INE917I01010", "BOTH", "Bajaj Auto Limited",               "AUTO",        "Two-Wheelers",      260000,  45000,  20.0, 8000,  281.0, 33.0, 8.0, 25.0,  -30000,  0.88),
        ("HEROMOTOCO", "INE158A01026", "BOTH", "Hero MotoCorp Limited",            "AUTO",        "Two-Wheelers",      80000,   38000,  13.0, 4500,  224.0, 18.0, 4.5, 25.0,  -15000,  0.85),
        ("EICHERMOT",  "INE066A01021", "BOTH", "Eicher Motors Limited",            "AUTO",        "Motorcycles & CVs", 120000,  17500,  25.0, 4100,  151.0, 30.0, 7.0, 25.0,  -20000,  0.85),
        ("TATACONSUM", "INE192A01025", "BOTH", "Tata Consumer Products Limited",   "FMCG",        "Tea & Food",       85000,   15500,  13.0, 1500,  17.0,  57.0, 4.5, 8.0,   5000,    0.80),
        ("HINDALCO",   "INE038A01020", "BOTH", "Hindalco Industries Limited",      "METALS",      "Aluminium",         130000,  220000, 12.0, 10000, 44.0,  13.0, 1.4, 12.0,  60000,   0.82),
        ("TATASTEEL",  "INE081A01012", "BOTH", "Tata Steel Limited",               "METALS",      "Steel",             160000,  230000, 12.0, 8000,  64.0,  20.0, 1.4, 8.0,   90000,   0.80),
        ("JSWSTEEL",   "INE019A01038", "BOTH", "JSW Steel Limited",                "METALS",      "Steel",             220000,  170000, 15.0, 9000,  37.0,  24.0, 3.0, 14.0,  80000,   0.82),
        ("SBILIFE",    "INE123W01016", "BOTH", "SBI Life Insurance Company",       "INSURANCE",   "Life Insurance",   160000,  85000,  10.0, 2200,  22.0,  73.0, 10.0, 15.0, -10000,  0.82),
        ("HDFCLIFE",   "INE795G01014", "BOTH", "HDFC Life Insurance Company",      "INSURANCE",   "Life Insurance",   140000,  55000,  10.0, 1800,  8.5,   78.0, 10.0, 12.0, -8000,   0.82),
        ("ICICIPRULI", "INE726G01019", "BOTH", "ICICI Prudential Life Insurance",  "INSURANCE",   "Life Insurance",   100000,  48000,  10.0, 1500,  10.5,  67.0, 8.0,  12.0,  -5000,   0.80),
        ("HDFCAMC",    "INE127D01025", "BOTH", "HDFC Asset Management Company",    "FINANCE",     "Asset Management",  80000,  3200,   62.0, 1900,  90.0,  42.0, 12.0, 30.0, -5000,   0.85),
    ]

    async with AsyncSessionLocal() as session:
        added = 0
        skipped = 0
        for row in COMPANIES:
            (symbol, isin, exchange, name, sector, industry,
             mcap, rev, ebitda, pat, eps, pe, pb, roe, net_debt, dq) = row

            result = await session.execute(select(Company).where(Company.symbol == symbol))
            company = result.scalar_one_or_none()
            if company:
                skipped += 1
                continue

            company = Company(
                symbol=symbol, isin=isin, exchange=exchange,
                company_name=name,
                aliases=[name.split()[0], symbol],
                sector_nse=sector, industry_nse=industry,
                bse_group="A",
                mcap_cr=float(mcap),
                revenue_ttm_cr=float(rev),
                ebitda_margin_pct=float(ebitda),
                pat_cr=float(pat),
                eps_ttm=float(eps),
                pe_ratio=float(pe),
                pb_ratio=float(pb),
                roe_pct=float(roe),
                net_debt_cr=float(net_debt),
                is_active=True,
                data_quality_score=float(dq),
                financials_updated_at=datetime.now(timezone.utc),
            )
            session.add(company)
            added += 1

        await session.commit()
        logger.info(f"Seeded {added} companies, skipped {skipped} already-present")


async def sync_nse_all():
    """Fetch all NSE-listed companies from NSE equity master and upsert into KB."""
    from bharat_terminal.kb.sync.nse_sync import fetch_nse_company_list

    companies = await fetch_nse_company_list()
    logger.info(f"Fetched {len(companies)} companies from NSE")

    async with AsyncSessionLocal() as session:
        added = 0
        skipped = 0
        for c in companies:
            symbol = c["symbol"]
            result = await session.execute(select(Company).where(Company.symbol == symbol))
            existing = result.scalar_one_or_none()
            if existing:
                skipped += 1
                continue

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
                    from datetime import date
                    company.listing_date = datetime.strptime(c["listing_date"], "%d-%b-%Y").date()
                except (ValueError, TypeError):
                    pass

            session.add(company)
            added += 1

            if added % 100 == 0:
                await session.flush()
                logger.info(f"  flushed {added} so far...")

        await session.commit()
        logger.info(f"NSE sync done: added {added}, skipped {skipped} existing")


async def refresh_dcf_all():
    """Recompute DCF for every company that has revenue data."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(Company.revenue_ttm_cr.isnot(None), Company.is_active == True)
        )
        companies = result.scalars().all()
        symbols = [c.symbol for c in companies]

    logger.info(f"Running DCF for {len(symbols)} companies with financial data")
    ok = 0
    failed = 0
    for sym in symbols:
        try:
            await refresh_dcf(sym)
            ok += 1
        except Exception as e:
            logger.warning(f"DCF failed for {sym}: {e}")
            failed += 1
    logger.info(f"refresh-dcf-all done: {ok} ok, {failed} failed")


async def seed_relationships():
    """Seed known competitive and supply-chain relationships for major NSE companies."""
    # (source, target, target_name, relationship_type, weight)
    RELS = [
        # ── Banking ──────────────────────────────────────────────────────────
        ("HDFCBANK",   "ICICIBANK",  "ICICI Bank Limited",             "COMPETITOR", 0.9),
        ("HDFCBANK",   "AXISBANK",   "Axis Bank Limited",              "COMPETITOR", 0.8),
        ("HDFCBANK",   "SBIN",       "State Bank of India",            "COMPETITOR", 0.8),
        ("HDFCBANK",   "KOTAKBANK",  "Kotak Mahindra Bank Limited",    "COMPETITOR", 0.8),
        ("ICICIBANK",  "HDFCBANK",   "HDFC Bank Limited",              "COMPETITOR", 0.9),
        ("ICICIBANK",  "AXISBANK",   "Axis Bank Limited",              "COMPETITOR", 0.8),
        ("ICICIBANK",  "SBIN",       "State Bank of India",            "COMPETITOR", 0.7),
        ("AXISBANK",   "HDFCBANK",   "HDFC Bank Limited",              "COMPETITOR", 0.8),
        ("AXISBANK",   "ICICIBANK",  "ICICI Bank Limited",             "COMPETITOR", 0.8),
        ("AXISBANK",   "KOTAKBANK",  "Kotak Mahindra Bank Limited",    "COMPETITOR", 0.7),
        ("KOTAKBANK",  "HDFCBANK",   "HDFC Bank Limited",              "COMPETITOR", 0.8),
        ("KOTAKBANK",  "ICICIBANK",  "ICICI Bank Limited",             "COMPETITOR", 0.8),
        ("SBIN",       "HDFCBANK",   "HDFC Bank Limited",              "COMPETITOR", 0.8),
        ("SBIN",       "ICICIBANK",  "ICICI Bank Limited",             "COMPETITOR", 0.7),
        ("SBIN",       "AXISBANK",   "Axis Bank Limited",              "COMPETITOR", 0.7),
        ("INDUSINDBK", "AXISBANK",   "Axis Bank Limited",              "COMPETITOR", 0.7),
        ("INDUSINDBK", "KOTAKBANK",  "Kotak Mahindra Bank Limited",    "COMPETITOR", 0.6),
        # ── IT Services ──────────────────────────────────────────────────────
        ("TCS",        "INFY",       "Infosys Limited",                "COMPETITOR", 0.9),
        ("TCS",        "WIPRO",      "Wipro Limited",                  "COMPETITOR", 0.8),
        ("TCS",        "HCLTECH",    "HCL Technologies Limited",       "COMPETITOR", 0.8),
        ("TCS",        "TECHM",      "Tech Mahindra Limited",          "COMPETITOR", 0.7),
        ("TCS",        "LTIM",       "LTIMindtree Limited",            "COMPETITOR", 0.7),
        ("INFY",       "TCS",        "Tata Consultancy Services",      "COMPETITOR", 0.9),
        ("INFY",       "WIPRO",      "Wipro Limited",                  "COMPETITOR", 0.8),
        ("INFY",       "HCLTECH",    "HCL Technologies Limited",       "COMPETITOR", 0.8),
        ("WIPRO",      "TCS",        "Tata Consultancy Services",      "COMPETITOR", 0.8),
        ("WIPRO",      "INFY",       "Infosys Limited",                "COMPETITOR", 0.8),
        ("WIPRO",      "HCLTECH",    "HCL Technologies Limited",       "COMPETITOR", 0.7),
        ("HCLTECH",    "TCS",        "Tata Consultancy Services",      "COMPETITOR", 0.8),
        ("HCLTECH",    "INFY",       "Infosys Limited",                "COMPETITOR", 0.8),
        ("TECHM",      "WIPRO",      "Wipro Limited",                  "COMPETITOR", 0.7),
        ("LTIM",       "TCS",        "Tata Consultancy Services",      "COMPETITOR", 0.7),
        # ── Automobile ───────────────────────────────────────────────────────
        ("TATAMOTORS", "MARUTI",     "Maruti Suzuki India Limited",    "COMPETITOR", 0.8),
        ("TATAMOTORS", "M&M",        "Mahindra and Mahindra Limited",  "COMPETITOR", 0.8),
        ("MARUTI",     "TATAMOTORS", "Tata Motors Limited",            "COMPETITOR", 0.8),
        ("MARUTI",     "M&M",        "Mahindra and Mahindra Limited",  "COMPETITOR", 0.7),
        ("M&M",        "TATAMOTORS", "Tata Motors Limited",            "COMPETITOR", 0.8),
        ("M&M",        "MARUTI",     "Maruti Suzuki India Limited",    "COMPETITOR", 0.7),
        ("BAJAJ-AUTO", "HEROMOTOCO", "Hero MotoCorp Limited",          "COMPETITOR", 0.9),
        ("BAJAJ-AUTO", "EICHERMOT",  "Eicher Motors Limited",          "COMPETITOR", 0.7),
        ("HEROMOTOCO", "BAJAJ-AUTO", "Bajaj Auto Limited",             "COMPETITOR", 0.9),
        ("HEROMOTOCO", "EICHERMOT",  "Eicher Motors Limited",          "COMPETITOR", 0.7),
        ("EICHERMOT",  "BAJAJ-AUTO", "Bajaj Auto Limited",             "COMPETITOR", 0.7),
        # ── FMCG ─────────────────────────────────────────────────────────────
        ("HINDUNILVR", "ITC",        "ITC Limited",                    "COMPETITOR", 0.7),
        ("HINDUNILVR", "BRITANNIA",  "Britannia Industries Limited",   "COMPETITOR", 0.5),
        ("ITC",        "HINDUNILVR", "Hindustan Unilever Limited",     "COMPETITOR", 0.7),
        ("ITC",        "BRITANNIA",  "Britannia Industries Limited",   "COMPETITOR", 0.6),
        ("BRITANNIA",  "HINDUNILVR", "Hindustan Unilever Limited",     "COMPETITOR", 0.5),
        ("TATACONSUM", "HINDUNILVR", "Hindustan Unilever Limited",     "COMPETITOR", 0.6),
        # ── Pharma ───────────────────────────────────────────────────────────
        ("SUNPHARMA",  "DRREDDY",    "Dr. Reddy's Laboratories",       "COMPETITOR", 0.8),
        ("SUNPHARMA",  "CIPLA",      "Cipla Limited",                  "COMPETITOR", 0.8),
        ("SUNPHARMA",  "DIVISLAB",   "Divi's Laboratories Limited",    "COMPETITOR", 0.6),
        ("DRREDDY",    "SUNPHARMA",  "Sun Pharmaceutical Industries",  "COMPETITOR", 0.8),
        ("DRREDDY",    "CIPLA",      "Cipla Limited",                  "COMPETITOR", 0.7),
        ("CIPLA",      "SUNPHARMA",  "Sun Pharmaceutical Industries",  "COMPETITOR", 0.8),
        ("CIPLA",      "DRREDDY",    "Dr. Reddy's Laboratories",       "COMPETITOR", 0.7),
        ("DIVISLAB",   "SUNPHARMA",  "Sun Pharmaceutical Industries",  "COMPETITOR", 0.6),
        # ── Oil & Gas ─────────────────────────────────────────────────────────
        ("ONGC",       "RELIANCE",   "Reliance Industries Limited",    "COMPETITOR", 0.6),
        ("ONGC",       "BPCL",       "Bharat Petroleum Corporation",   "CUSTOMER",   0.6),
        ("ONGC",       "IOC",        "Indian Oil Corporation",         "CUSTOMER",   0.7),
        ("BPCL",       "IOC",        "Indian Oil Corporation",         "COMPETITOR", 0.9),
        ("BPCL",       "ONGC",       "Oil and Natural Gas Corporation","SUPPLIER",   0.6),
        ("IOC",        "BPCL",       "Bharat Petroleum Corporation",   "COMPETITOR", 0.9),
        ("IOC",        "ONGC",       "Oil and Natural Gas Corporation","SUPPLIER",   0.7),
        # ── Power ─────────────────────────────────────────────────────────────
        ("NTPC",       "POWERGRID",  "Power Grid Corporation of India","CUSTOMER",   0.8),
        ("POWERGRID",  "NTPC",       "NTPC Limited",                   "SUPPLIER",   0.8),
        ("NTPC",       "COALINDIA",  "Coal India Limited",             "SUPPLIER",   0.7),
        ("COALINDIA",  "NTPC",       "NTPC Limited",                   "CUSTOMER",   0.7),
        # ── Cement ────────────────────────────────────────────────────────────
        ("ULTRACEMCO", "SHREECEM",   "Shree Cement Limited",           "COMPETITOR", 0.9),
        ("ULTRACEMCO", "GRASIM",     "Grasim Industries Limited",      "COMPETITOR", 0.7),
        ("SHREECEM",   "ULTRACEMCO", "UltraTech Cement Limited",       "COMPETITOR", 0.9),
        ("GRASIM",     "ULTRACEMCO", "UltraTech Cement Limited",       "COMPETITOR", 0.7),
        # ── Metals ────────────────────────────────────────────────────────────
        ("TATASTEEL",  "JSWSTEEL",   "JSW Steel Limited",              "COMPETITOR", 0.9),
        ("TATASTEEL",  "HINDALCO",   "Hindalco Industries Limited",    "COMPETITOR", 0.5),
        ("JSWSTEEL",   "TATASTEEL",  "Tata Steel Limited",             "COMPETITOR", 0.9),
        ("HINDALCO",   "TATASTEEL",  "Tata Steel Limited",             "COMPETITOR", 0.5),
        # ── Telecom ───────────────────────────────────────────────────────────
        ("BHARTIARTL", "JIOFINANCE", "Jio Financial Services Limited", "COMPETITOR", 0.5),
        # ── Infra / Conglomerate ──────────────────────────────────────────────
        ("LT",         "ADANIENT",   "Adani Enterprises Limited",      "COMPETITOR", 0.6),
        ("ADANIENT",   "LT",         "Larsen and Toubro Limited",      "COMPETITOR", 0.6),
        ("ADANIPORTS", "ADANIENT",   "Adani Enterprises Limited",      "PARENT",     0.8),
        # ── Finance ───────────────────────────────────────────────────────────
        ("BAJFINANCE",  "BAJAJFINSV","Bajaj Finserv Limited",          "SUBSIDIARY", 0.8),
        ("BAJAJFINSV",  "BAJFINANCE","Bajaj Finance Limited",          "PARENT",     0.8),
        ("HDFCAMC",     "HDFCBANK",  "HDFC Bank Limited",              "PARENT",     0.7),
        ("SBILIFE",     "SBIN",      "State Bank of India",            "PARENT",     0.7),
        ("HDFCLIFE",    "HDFCBANK",  "HDFC Bank Limited",              "PARENT",     0.7),
        ("ICICIPRULI",  "ICICIBANK", "ICICI Bank Limited",             "PARENT",     0.7),
        # ── Consumer ─────────────────────────────────────────────────────────
        ("TITAN",       "HINDUNILVR","Hindustan Unilever Limited",     "COMPETITOR", 0.3),
        ("ASIANPAINT",  "HINDUNILVR","Hindustan Unilever Limited",     "COMPETITOR", 0.3),
    ]

    async with AsyncSessionLocal() as session:
        added = 0
        skipped = 0
        for src, tgt, tgt_name, rel_type, weight in RELS:
            existing = await session.execute(
                select(CompanyRelationship).where(
                    CompanyRelationship.source_symbol == src,
                    CompanyRelationship.target_symbol == tgt,
                    CompanyRelationship.relationship_type == rel_type,
                )
            )
            if existing.scalar_one_or_none():
                skipped += 1
                continue
            session.add(CompanyRelationship(
                source_symbol=src,
                target_symbol=tgt,
                target_name=tgt_name,
                relationship_type=rel_type,
                weight=weight,
                source_of_truth="manual",
                confidence=0.9,
            ))
            added += 1

        await session.commit()
        logger.info(f"seed-relationships done: {added} added, {skipped} already present")


async def create_tables():
    """Create all database tables (for development)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Tables created")


def main():
    parser = argparse.ArgumentParser(description="Bharat Terminal KB Management CLI")
    subparsers = parser.add_subparsers(dest="command")

    dcf_parser = subparsers.add_parser("refresh-dcf", help="Refresh DCF model for a symbol")
    dcf_parser.add_argument("--symbol", required=True, help="NSE symbol e.g. RELIANCE")

    subparsers.add_parser("refresh-dcf-all", help="Refresh DCF for all companies with financial data")
    subparsers.add_parser("seed-relationships", help="Seed known company relationships for major NSE companies")
    subparsers.add_parser("seed-reliance", help="Seed RELIANCE with test data")
    subparsers.add_parser("seed-nifty50", help="Seed major NIFTY 50 companies")
    subparsers.add_parser("sync-nse", help="Fetch all NSE-listed companies from NSE equity master")
    subparsers.add_parser("create-tables", help="Create all database tables")

    args = parser.parse_args()

    if args.command == "refresh-dcf":
        asyncio.run(refresh_dcf(args.symbol))
    elif args.command == "refresh-dcf-all":
        asyncio.run(refresh_dcf_all())
    elif args.command == "seed-relationships":
        asyncio.run(seed_relationships())
    elif args.command == "seed-reliance":
        asyncio.run(seed_reliance())
    elif args.command == "seed-nifty50":
        asyncio.run(seed_nifty50())
    elif args.command == "sync-nse":
        asyncio.run(sync_nse_all())
    elif args.command == "create-tables":
        asyncio.run(create_tables())
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

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

    subparsers.add_parser("seed-reliance", help="Seed RELIANCE with test data")
    subparsers.add_parser("create-tables", help="Create all database tables")

    args = parser.parse_args()

    if args.command == "refresh-dcf":
        asyncio.run(refresh_dcf(args.symbol))
    elif args.command == "seed-reliance":
        asyncio.run(seed_reliance())
    elif args.command == "create-tables":
        asyncio.run(create_tables())
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

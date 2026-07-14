"""
SQLAlchemy ORM models for Bharat Terminal knowledge base.
Uses PostgreSQL + pgvector extension.
"""
from datetime import datetime, date
from typing import Optional, List

from sqlalchemy import (
    String, Integer, BigInteger, Float, Numeric, Boolean, DateTime, Date, Text,
    ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import DeclarativeBase, relationship, Mapped, mapped_column
from sqlalchemy.sql import func
from pgvector.sqlalchemy import Vector


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identity fields
    symbol: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    isin: Mapped[Optional[str]] = mapped_column(String(12), unique=True, nullable=True, index=True)
    exchange: Mapped[str] = mapped_column(String(5), nullable=False)  # "NSE" | "BSE" | "BOTH"
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases: Mapped[Optional[List[str]]] = mapped_column(JSONB, nullable=True)  # list of strings
    sector_nse: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    industry_nse: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    bse_group: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)  # A/B/T/Z
    # 6-digit BSE security code (api.bseindia.com key). Unique via partial
    # index ix_companies_bse_scrip_code (migration 019) — NULL for NSE-only rows.
    bse_scrip_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, index=True)
    mcap_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Market cap in Crores
    listing_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # Business description
    description_200w: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    revenue_segments: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Format: [{"name": "...", "pct_of_revenue": 40.0, "yoy_growth": 12.0}]
    geography_split: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Format: {"India": 85.0, "USA": 10.0, "Others": 5.0}
    moat_classification: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # "COST_ADVANTAGE" | "SWITCHING_COSTS" | "NETWORK_EFFECTS" | "INTANGIBLE_ASSETS" | "NONE"

    # Financial metrics (TTM)
    revenue_ttm_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ebitda_margin_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pat_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    eps_ttm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pb_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    roe_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_debt_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    interest_coverage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fcf_yield_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    financials_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # DCF model
    wacc_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    terminal_growth_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    projection_years: Mapped[int] = mapped_column(Integer, default=5)
    year_projections: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Format: [{"year": 1, "revenue": ..., "ebitda": ..., "pat": ..., "eps": ..., "fcf": ...}, ...]
    fair_value_per_share: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    margin_of_safety_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dcf_bull_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dcf_bear_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dcf_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    dcf_source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # "screener_api" | "llm_inferred" | "manual"
    dcf_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # 0.0 - 1.0, lower for llm_inferred

    # Analyst consensus
    median_target_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    buy_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hold_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sell_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    num_analysts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    eps_fy_curr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    eps_fy_next: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    consensus_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Vector embedding for entity resolution
    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(768), nullable=True)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    data_quality_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # 0.0 (stub) - 1.0 (fully populated with verified data)

    # Relationships
    relationships_as_source: Mapped[List["CompanyRelationship"]] = relationship(
        "CompanyRelationship", foreign_keys="CompanyRelationship.source_symbol",
        back_populates="source_company", lazy="select"
    )
    price_history: Mapped[List["PricePoint"]] = relationship(
        "PricePoint", back_populates="company", lazy="select"
    )

    __table_args__ = (
        Index("ix_companies_sector", "sector_nse"),
        Index("ix_companies_mcap", "mcap_cr"),
        Index("ix_companies_exchange", "exchange"),
    )

    def to_dict(self) -> dict:
        return {
            "identity": {
                "symbol": self.symbol,
                "isin": self.isin,
                "exchange": self.exchange,
                "company_name": self.company_name,
                "aliases": self.aliases or [],
                "sector_nse": self.sector_nse,
                "industry_nse": self.industry_nse,
                "bse_group": self.bse_group,
                "mcap_cr": self.mcap_cr,
                "listing_date": self.listing_date.isoformat() if self.listing_date else None,
            },
            "business": {
                "description_200w": self.description_200w,
                "revenue_segments": self.revenue_segments or [],
                "geography_split": self.geography_split or {},
                "moat_classification": self.moat_classification,
            },
            "financials": {
                "revenue_ttm_cr": self.revenue_ttm_cr,
                "ebitda_margin_pct": self.ebitda_margin_pct,
                "pat_cr": self.pat_cr,
                "eps_ttm": self.eps_ttm,
                "pe_ratio": self.pe_ratio,
                "pb_ratio": self.pb_ratio,
                "roe_pct": self.roe_pct,
                "net_debt_cr": self.net_debt_cr,
                "interest_coverage": self.interest_coverage,
                "fcf_yield_pct": self.fcf_yield_pct,
                "updated_at": self.financials_updated_at.isoformat() if self.financials_updated_at else None,
            },
            "dcf_model": {
                "wacc_pct": self.wacc_pct,
                "terminal_growth_pct": self.terminal_growth_pct,
                "projection_years": self.projection_years,
                "year_projections": self.year_projections or [],
                "fair_value_per_share": self.fair_value_per_share,
                "margin_of_safety_pct": self.margin_of_safety_pct,
                "bull_value": self.dcf_bull_value,
                "bear_value": self.dcf_bear_value,
                "last_updated": self.dcf_updated_at.isoformat() if self.dcf_updated_at else None,
                "source": self.dcf_source,
                "confidence": self.dcf_confidence,
            },
            "analyst_consensus": {
                "median_target_price": self.median_target_price,
                "buy_pct": self.buy_pct,
                "hold_pct": self.hold_pct,
                "sell_pct": self.sell_pct,
                "num_analysts": self.num_analysts,
                "eps_fy_curr": self.eps_fy_curr,
                "eps_fy_next": self.eps_fy_next,
                "updated_at": self.consensus_updated_at.isoformat() if self.consensus_updated_at else None,
            },
        }


class CompanyRelationship(Base):
    __tablename__ = "company_relationships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    target_symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    target_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    relationship_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    # "SUPPLIER" | "CUSTOMER" | "COMPETITOR" | "JV" | "SUBSIDIARY" | "PARENT"
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    # 0.0 - 1.0: strength of relationship
    source_of_truth: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # "annual_report" | "screener" | "llm_inferred" | "manual"
    confidence: Mapped[float] = mapped_column(Float, default=0.8, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source_company: Mapped["Company"] = relationship(
        "Company", foreign_keys=[source_symbol], back_populates="relationships_as_source"
    )

    __table_args__ = (
        UniqueConstraint("source_symbol", "target_symbol", "relationship_type", name="uq_relationship"),
        Index("ix_relationships_source", "source_symbol"),
        Index("ix_relationships_target", "target_symbol"),
    )


class PricePoint(Base):
    __tablename__ = "price_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Optional[float]] = mapped_column(Float)
    high: Mapped[Optional[float]] = mapped_column(Float)
    low: Mapped[Optional[float]] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[Optional[int]] = mapped_column(BigInteger)

    company: Mapped["Company"] = relationship("Company", back_populates="price_history")

    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_price_point"),
        Index("ix_prices_symbol_date", "symbol", "date"),
    )


class NseFiling(Base):
    """Metadata from NSE's corporates-financial-results API. One row per filing."""
    __tablename__ = "nse_filings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    isin: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    period: Mapped[str] = mapped_column(String(20), nullable=False)
    relating_to: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    financial_year: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    consolidated: Mapped[str] = mapped_column(String(30), nullable=False)
    audited: Mapped[str] = mapped_column(String(20), nullable=False)
    ind_as: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    format: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    xbrl_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detail_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    seq_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    filing_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    broadcast_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    xbrl_downloaded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    xbrl_parsed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    financial_results: Mapped[List["FinancialResult"]] = relationship(
        "FinancialResult", back_populates="nse_filing", lazy="select"
    )

    __table_args__ = (
        UniqueConstraint("symbol", "from_date", "to_date", "consolidated", name="uq_nse_filing"),
        Index("ix_nse_filings_symbol", "symbol"),
        Index("ix_nse_filings_to_date", "to_date"),
        Index("ix_nse_filings_parsed", "xbrl_parsed"),
    )


class FinancialResult(Base):
    """Quarterly/annual P&L data parsed from XBRL. One row per company per period."""
    __tablename__ = "financial_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_type: Mapped[str] = mapped_column(String(20), nullable=False)
    consolidated: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    audited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # P&L line items (Crores INR)
    revenue_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    other_income_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_income_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cost_of_materials_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    purchases_stock_in_trade_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    inventory_changes_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    employee_expense_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    finance_costs_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    depreciation_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    other_expenses_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_expenses_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pbt_before_exceptional_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exceptional_items_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pbt_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tax_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pat_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pat_attributable_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    other_comprehensive_income_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # EPS
    eps_basic: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    eps_diluted: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    face_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    paid_up_capital_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Ratios
    debt_equity_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    interest_coverage_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Derived
    ebitda_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ebitda_margin_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Segment data
    segment_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # YTD cumulative
    ytd_revenue_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ytd_pat_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ytd_eps_basic: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Provenance
    source: Mapped[str] = mapped_column(String(20), default="nse_xbrl", nullable=False)
    nse_filing_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("nse_filings.id"), nullable=True
    )
    # When the result was ANNOUNCED to the exchange (drift measurement anchors
    # here, not at period_end). NSE: filing/broadcast date; BSE: results-index
    # timestamp. Indexed via ix_financial_results_announced (migration 020).
    announced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw_xbrl_facts: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Accounting taxonomy this row was filed under (indas / nbfc / banking /
    # life_insurance / general_insurance / nonindas). Tells consumers how to
    # interpret the common columns and which sector_financials keys exist.
    taxonomy: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, index=True)
    # Sector-specific P&L lines that have no generic column (e.g. banks'
    # interest_expended / provisions, insurers' claims_incurred / net_commission).
    # NULL for general (Ind AS / NBFC / non-Ind AS) companies whose P&L maps to
    # the typed columns above. Normalized snake_case keys, values in Crores.
    sector_financials: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    company: Mapped["Company"] = relationship("Company", foreign_keys=[symbol], lazy="select")
    nse_filing: Mapped[Optional["NseFiling"]] = relationship(
        "NseFiling", back_populates="financial_results", lazy="select"
    )

    __table_args__ = (
        UniqueConstraint("symbol", "period_end", "period_type", "consolidated", name="uq_financial_result"),
        Index("ix_financial_results_symbol", "symbol"),
        Index("ix_financial_results_period", "period_end"),
        Index("ix_financial_results_symbol_period", "symbol", "period_end"),
    )


class EarningsSurprise(Base):
    """One row per (symbol, quarter): standardized earnings surprise (SUE)
    computed at results ingest against the company's own history, with the
    trailing-distribution decile and liquidity screen. Frozen recipe from the
    validated PEAD study (study_version, see research/pead_signals.py).
    Deciles 10/1 with liquidity_pass emit deterministic signal_outcomes rows
    (source='PEAD_SUE') for live out-of-sample tracking."""
    __tablename__ = "earnings_surprises"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    announced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    eps_basic: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    revenue_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sue_eps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sue_revenue: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sue_composite: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    percentile: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    decile: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    liquidity_pass: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    median_traded_value_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reference_n: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    study_version: Mapped[str] = mapped_column(String(20), nullable=False)
    signal_emitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("symbol", "period_end", name="uq_earnings_surprise"),
    )


class ShareholdingPattern(Base):
    __tablename__ = "shareholding_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    quarter_end: Mapped[date] = mapped_column(Date, nullable=False)
    promoter_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    promoter_pledge_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fii_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dii_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    public_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    num_shareholders: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="nse", nullable=False)
    raw_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("symbol", "quarter_end", name="uq_shareholding"),
        Index("ix_shareholding_symbol", "symbol"),
        Index("ix_shareholding_quarter", "quarter_end"),
    )


class InvestorHolding(Base):
    """One row per (company, quarter, named >=1% shareholder) from the SHP XBRL
    detail table. Powers stock-centric notable-holders + investor-centric
    portfolio/QoQ views (migration 021)."""
    __tablename__ = "investor_holdings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    quarter_end: Mapped[date] = mapped_column(Date, nullable=False)
    holder_name: Mapped[str] = mapped_column(Text, nullable=False)
    holder_name_norm: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    category: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    holding_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    shares_held: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("symbol", "quarter_end", "holder_name", name="uq_investor_holding"),
        Index("ix_investor_holdings_symbol_q", "symbol", "quarter_end"),
    )


class MarketDeal(Base):
    """One disclosed bulk/block deal (NSE+BSE). Market-making/HFT churn is FLAGGED
    (is_excluded + exclude_reason), not deleted, so the filter stays auditable
    (migration 022)."""
    __tablename__ = "market_deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deal_date: Mapped[date] = mapped_column(Date, nullable=False)
    exchange: Mapped[str] = mapped_column(String(4), nullable=False)
    deal_type: Mapped[str] = mapped_column(String(8), nullable=False)  # bulk | block
    symbol: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    company_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    client_name: Mapped[str] = mapped_column(Text, nullable=False)
    client_name_norm: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY | SELL
    quantity: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    value_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    marquee_key: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    is_excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclude_reason: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("deal_date", "exchange", "deal_type", "client_name",
                         "symbol", "side", "quantity", "price", name="uq_market_deal"),
        Index("ix_market_deals_symbol_date", "symbol", "deal_date"),
        Index("ix_market_deals_norm_date", "client_name_norm", "deal_date"),
    )


class CorporateAction(Base):
    __tablename__ = "corporate_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    ex_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    record_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    action_type: Mapped[str] = mapped_column(String(20), nullable=False)
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="nse", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("symbol", "ex_date", "action_type", name="uq_corporate_action"),
        Index("ix_corp_actions_symbol", "symbol"),
        Index("ix_corp_actions_date", "ex_date"),
    )


class ResultEvent(Base):
    """Tracks scheduled financial result announcements from NSE board meetings calendar."""
    __tablename__ = "result_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    result_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False
    )  # pending | synced | failed | no_data
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("symbol", "result_date", name="uq_result_event"),
        Index("ix_result_events_date", "result_date"),
        Index("ix_result_events_status", "sync_status"),
    )


class Trigger(Base):
    """A quantitative input variable we calibrate equity betas against."""
    __tablename__ = "triggers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)         # continuous | discrete
    unit: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    data_source: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MacroEvent(Base):
    """A macro event whose propagation we model across 3 degrees."""
    __tablename__ = "macro_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    trigger_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("triggers.id"), nullable=False
    )
    trigger_spec: Mapped[str] = mapped_column(Text, nullable=False)
    propagation_chain: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EventDegree(Base):
    __tablename__ = "event_degrees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("macro_events.id", ondelete="CASCADE"), nullable=False
    )
    degree: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    transmission_channel: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("event_id", "degree", name="uq_event_degrees"),
    )


class EventSectorEdge(Base):
    __tablename__ = "event_sector_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("macro_events.id", ondelete="CASCADE"), nullable=False
    )
    degree: Mapped[int] = mapped_column(Integer, nullable=False)
    sector: Mapped[str] = mapped_column(String(120), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    magnitude_prior: Mapped[int] = mapped_column(Integer, nullable=False)
    lag_text: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    lag_min_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    lag_max_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mechanism: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("event_id", "degree", "sector", name="uq_event_sector_edge"),
        Index("ix_event_sector_edges_event", "event_id"),
        Index("ix_event_sector_edges_sector", "sector"),
    )


class EventTransmission(Base):
    __tablename__ = "event_transmissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("macro_events.id", ondelete="CASCADE"), nullable=False
    )
    from_degree: Mapped[int] = mapped_column(Integer, nullable=False)
    to_degree: Mapped[int] = mapped_column(Integer, nullable=False)
    gating_trigger_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("triggers.id"), nullable=False
    )
    confirms_threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    refutes_threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "event_id", "from_degree", "to_degree", "gating_trigger_id",
            name="uq_event_transmission"
        ),
    )


class TriggerBeta(Base):
    """
    Calibrated beta of a stock's return to a 1-unit move in a trigger.

    Multiple rows per (symbol, trigger) are allowed — one per derivation
    method. The signal layer picks by priority:
      regression > agent_research > manual
    so that data-driven calibrations win when they exist.
    """
    __tablename__ = "trigger_betas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    trigger_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("triggers.id"), nullable=False
    )
    beta: Mapped[float] = mapped_column(Float, nullable=False)
    beta_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    beta_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    std_err: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    t_stat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    r_squared: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    n_observations: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sample_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    sample_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    derivation_method: Mapped[str] = mapped_column(String(40), nullable=False)
    model_used: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False, default="low")
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    citations: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    calibrated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "symbol", "trigger_id", "derivation_method", name="uq_trigger_beta_method"
        ),
        Index("ix_trigger_betas_symbol_trigger", "symbol", "trigger_id"),
        Index("ix_trigger_betas_trigger", "trigger_id"),
    )


class FundamentalExposure(Base):
    __tablename__ = "fundamental_exposures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    trigger_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("triggers.id"), nullable=False
    )
    exposure_kind: Mapped[str] = mapped_column(String(20), nullable=False)  # cost | revenue | mixed
    pct_of_total: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_basis: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    mechanism: Mapped[str] = mapped_column(Text, nullable=False)
    source_doc_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_period: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    source_kind: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    citations: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "symbol", "trigger_id", "source_period", name="uq_fundamental_exposure"
        ),
        Index("ix_fundamental_exposures_symbol", "symbol"),
        Index("ix_fundamental_exposures_trigger", "trigger_id"),
    )


class SectorLeader(Base):
    __tablename__ = "sector_leaders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("macro_events.id", ondelete="CASCADE"), nullable=False
    )
    degree: Mapped[int] = mapped_column(Integer, nullable=False)
    sector: Mapped[str] = mapped_column(String(120), nullable=False)
    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    derivation_method: Mapped[str] = mapped_column(String(40), nullable=False)
    last_calibrated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "event_id", "degree", "sector", "symbol", name="uq_sector_leader"
        ),
        Index("ix_sector_leaders_event", "event_id", "degree"),
    )


class UniverseMembership(Base):
    __tablename__ = "universe_membership"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    universe_name: Mapped[str] = mapped_column(String(40), nullable=False)
    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    weight_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("universe_name", "symbol", name="uq_universe_member"),
        Index("ix_universe_universe", "universe_name"),
    )


# ── Dossier layer (migration 007) ────────────────────────────────────────


class CompanyDossier(Base):
    """
    Single source of truth: full CFA-level dossier per company.
    Generated by `research/dossier_agent.py` (Sonnet + web_search + BSE XBRL).
    Versioned and stamped with the latest financial-results quarter
    incorporated. The flat tables below (DossierSensitivity, DossierLinkage)
    are denormalised views of the JSONB sections for fast indexed queries.
    """
    __tablename__ = "company_dossiers"

    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[str] = mapped_column(String(20), nullable=False, server_default="1.0.0")
    source_quarter: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    generated_by: Mapped[str] = mapped_column(String(60), nullable=False, server_default="dossier-agent")
    model_used: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    refresh_kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="initial")

    business:        Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    financials:      Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    sensitivities:   Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    linkages:        Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    risks:           Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    catalysts:       Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    mgmt_commentary: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    citations:       Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    data_quality: Mapped[Optional[float]] = mapped_column(Numeric(3, 2), nullable=True)
    tokens_used:  Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)


class DossierSensitivity(Base):
    """Flattened sensitivity row — one per (symbol, trigger, channel)."""
    __tablename__ = "dossier_sensitivities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    trigger_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("triggers.id", ondelete="CASCADE"), nullable=False
    )
    beta: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    lag_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    confidence: Mapped[str] = mapped_column(String(10), nullable=False, server_default="med")
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("symbol", "trigger_id", "channel", name="uq_dossier_sens"),
    )


class DossierLinkage(Base):
    """Flattened company → company edge for graph traversal."""
    __tablename__ = "dossier_linkages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_symbol: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False
    )
    target_symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    strength: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False, server_default="positive")
    propagation_lag_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("source_symbol", "target_symbol", "kind", name="uq_dossier_linkage"),
    )


# ── Derivatives layer ────────────────────────────────────────────────────
# derivative_ticks + derivatives_eod were dropped in migration 016 (unused —
# no downstream consumer; the live F&O dataset is participant_oi below).


class ParticipantOI(Base):
    """Daily NSE FII/DII/Pro/Client positioning. Leading-indicator signal."""
    __tablename__ = "participant_oi"

    date:            Mapped[date]    = mapped_column(Date, primary_key=True)
    client_type:     Mapped[str]     = mapped_column(String(10), primary_key=True)
    instrument_type: Mapped[str]     = mapped_column(String(20), primary_key=True)
    long_contracts:  Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    short_contracts: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # net_contracts is a GENERATED column in PostgreSQL; expose read-only here
    net_contracts:   Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source_csv:      Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sync_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    records_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class MemoryItem(Base):
    """Distilled, embedded, DERIVED event-memory index (migration 015).

    One row per consolidated real-world event, materialised from impact_reports
    (near-duplicate coverage collapsed). Rebuildable from source, so it cannot
    drift; retrieval ranks by relevance (embedding) + recency + importance.
    """
    __tablename__ = "memory_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    sentiment: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    magnitude: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_horizon: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    macro_theme: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    symbols: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text), nullable=True)
    sectors: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text), nullable=True)
    trigger_ids: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text), nullable=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    importance: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    source_news_ids: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(768), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MemoryDigest(Base):
    """Overwrite-cached sector/market narrative digest (migration 015).

    One row per (scope_type, scope_key) — e.g. ('sector','BANKING') or
    ('market','MARKET') — overwritten on each refresh, so it is bounded by
    construction (~25 rows) and needs no eviction job.
    """
    __tablename__ = "memory_digests"

    scope_type: Mapped[str] = mapped_column(String(10), primary_key=True)  # 'sector' | 'market'
    scope_key: Mapped[str] = mapped_column(String(120), primary_key=True)  # sector code | 'MARKET'
    digest_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    net_tilt: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    source_window_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    input_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_used: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

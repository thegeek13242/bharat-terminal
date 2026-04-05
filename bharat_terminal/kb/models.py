"""
SQLAlchemy ORM models for Bharat Terminal knowledge base.
Uses PostgreSQL + pgvector extension.
"""
from datetime import datetime, date
from typing import Optional, List
import uuid

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Date, Text, JSON,
    ForeignKey, UniqueConstraint, Index, Enum as SAEnum, ARRAY
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
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
    volume: Mapped[Optional[int]] = mapped_column(Integer)

    company: Mapped["Company"] = relationship("Company", back_populates="price_history")

    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_price_point"),
        Index("ix_prices_symbol_date", "symbol", "date"),
    )

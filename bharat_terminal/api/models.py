"""
SQLAlchemy ORM models for API-layer persistence:
  - news_items    (raw ingest records)
  - impact_reports (analysis pipeline output)
"""
from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Boolean, DateTime, Text, Index
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
import uuid


class Base(DeclarativeBase):
    pass


class NewsItemRecord(Base):
    __tablename__ = "news_items"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    timestamp_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    ingest_latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    symbols_mentioned: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )


class ImpactReportRecord(Base):
    __tablename__ = "impact_reports"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    news_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), nullable=False, index=True
    )
    relevant: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    macro_theme: Mapped[str | None] = mapped_column(String(100), nullable=True)
    affected_sectors: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    company_impacts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    trade_signals: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    processing_latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True
    )


# Composite index for time-range + relevance queries
Index("ix_impact_reports_created_relevant", ImpactReportRecord.created_at, ImpactReportRecord.relevant)
Index("ix_news_items_source_ts", NewsItemRecord.source, NewsItemRecord.timestamp_utc)

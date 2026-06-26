"""
SQLAlchemy ORM models for API-layer persistence:
  - news_items    (raw ingest records)
  - impact_reports (analysis pipeline output)
"""
from datetime import datetime, date
from sqlalchemy import (
    String, Float, Boolean, DateTime, Date, Text, Index
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
import uuid


class Base(DeclarativeBase):
    pass


class NewsItemRecord(Base):
    __tablename__ = "news_items"

    # Text PK — ingestion IDs can be "SOURCE_url" strings, not always UUIDs
    id: Mapped[str] = mapped_column(Text, primary_key=True)
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

    id: Mapped[str] = mapped_column(Text, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    news_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
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


class SignalOutcomeRecord(Base):
    """Per-signal outcome tracking (Feature 2 — measure + flag).

    One row per (impact_report, symbol, direction) trade signal, given a stable
    synthetic `signal_id` derived from those three fields (signals are stored
    anonymously inside impact_reports.trade_signals JSONB). Populated + evaluated
    idempotently by `research/score_signals.py`; never mutates the emission path.
    """
    __tablename__ = "signal_outcomes"

    # sha256(impact_report_id|symbol|direction)[:32] — idempotent, re-derivable.
    signal_id: Mapped[str] = mapped_column(Text, primary_key=True)
    impact_report_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    news_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    conviction: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    instrument_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    macro_theme: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    trigger_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    signal_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Evaluation anchor + realized forward returns (percent) by trading-day horizon.
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ret_1d: Mapped[float | None] = mapped_column(Float, nullable=True)
    ret_5d: Mapped[float | None] = mapped_column(Float, nullable=True)
    ret_10d: Mapped[float | None] = mapped_column(Float, nullable=True)
    ret_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Direction-correct per horizon (NULL = inconclusive within the dead-band).
    correct_1d: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    correct_5d: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    correct_10d: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    correct_20d: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Implied move (percent) from the matching CompanyImpact.magnitude (a proxy);
    # magnitude error = abs(realized ret) - implied_move_pct, computed in queries.
    implied_move_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    eval_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # LLM post-mortem on wrong signals (separate, costed pass).
    failure_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    failure_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    postmortem_model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    postmortem_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Composite index for time-range + relevance queries
Index("ix_impact_reports_created_relevant", ImpactReportRecord.created_at, ImpactReportRecord.relevant)
Index("ix_news_items_source_ts", NewsItemRecord.source, NewsItemRecord.timestamp_utc)

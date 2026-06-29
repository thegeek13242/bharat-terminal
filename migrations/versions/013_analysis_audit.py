"""Add analysis_audit table — immutable compliance/audit record per analysis event

For audit and compliance we need a single, self-contained, append-only record of
every analysis the system produces: the news event that triggered it, the EXACT
input the model read (full_body — not the short RSS summary stored in
news_items.body), the complete analysis output (company impacts + trade signals),
and model provenance. The existing news_items / impact_reports tables are
operational (they drive the UI feed) and split this across rows with a lossy
input, so they are not audit-grade.

Each row is keyed by the impact-report id (idempotent: re-processing the same
report INSERTs ON CONFLICT DO NOTHING — never overwrites history). Written
atomically alongside news_items + impact_reports by api/kafka_relay.py from
analysed.impact.in.

Provenance columns (model_id, provider, prompt_version, tokens, pipeline_mode)
are nullable: the input/analysis/signals snapshot is captured immediately, while
the actual model/token values are threaded through the analysis pipeline in a
follow-up (no second migration needed).

Revision ID: 013
Revises: 012
Create Date: 2026-06-28 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_audit",
        # ── identity ──────────────────────────────────────────────────────
        # id == the impact_report id; join impact_reports ON id = analysis_audit.id
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("news_id", sa.Text(), nullable=False),

        # ── input snapshot (the exact thing the model saw) ────────────────
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("full_body", sa.Text(), nullable=True),
        sa.Column("body_word_count", sa.Integer(), nullable=True),
        sa.Column("body_fetch_status", sa.String(length=20), nullable=True),
        sa.Column("body_extraction_method", sa.String(length=30), nullable=True),

        # ── analysis output ───────────────────────────────────────────────
        sa.Column("relevant", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("macro_theme", sa.String(length=100), nullable=True),
        sa.Column("affected_sectors", postgresql.JSONB(), nullable=True),
        sa.Column("company_impacts", postgresql.JSONB(), nullable=True),
        sa.Column("trade_signals", postgresql.JSONB(), nullable=True),

        # ── model provenance (nullable now; populated by a follow-up) ──────
        sa.Column("model_id", sa.String(length=100), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=True),
        sa.Column("prompt_version", sa.String(length=40), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("pipeline_mode", sa.String(length=20), nullable=True),  # realtime | batch
        sa.Column("processing_latency_ms", sa.Float(), nullable=True),

        # ── tamper-evidence + bookkeeping ─────────────────────────────────
        # sha256 over a canonical JSON of (input snapshot + analysis output)
        sa.Column("content_hash", sa.Text(), nullable=True),
        # when the analysis ran (report.created_at)
        sa.Column("analysis_created_at", sa.DateTime(timezone=True), nullable=True),
        # when this audit row was written
        sa.Column("audited_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_analysis_audit_news_id", "analysis_audit", ["news_id"])
    op.create_index("ix_analysis_audit_source", "analysis_audit", ["source"])
    op.create_index("ix_analysis_audit_relevant", "analysis_audit", ["relevant"])
    op.create_index("ix_analysis_audit_model_id", "analysis_audit", ["model_id"])
    op.create_index("ix_analysis_audit_analysis_created_at", "analysis_audit",
                    ["analysis_created_at"])


def downgrade() -> None:
    op.drop_index("ix_analysis_audit_analysis_created_at", table_name="analysis_audit")
    op.drop_index("ix_analysis_audit_model_id", table_name="analysis_audit")
    op.drop_index("ix_analysis_audit_relevant", table_name="analysis_audit")
    op.drop_index("ix_analysis_audit_source", table_name="analysis_audit")
    op.drop_index("ix_analysis_audit_news_id", table_name="analysis_audit")
    op.drop_table("analysis_audit")

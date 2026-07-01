"""Add event-memory tables — memory_items (embedded, derived event index) and
memory_digests (overwrite-cached sector/market narrative).

`memory_items` is a DERIVED materialization of impact_reports: one row per
consolidated real-world event (near-duplicate coverage across outlets collapsed),
carrying the distilled summary, its sentiment/magnitude/horizon, the companies and
sectors it touched, an anchored event_time, and a pgvector embedding for
relevance retrieval. It is rebuildable from impact_reports at any time, so it can
never drift from the source of truth; the only "eviction" is a prune of rows past
a generous retention window (recoverable via a full rebuild).

`memory_digests` caches one short LLM-synthesised narrative per sector and one for
the whole market, keyed by (scope_type, scope_key) and OVERWRITTEN on each refresh
— bounded to ~25 rows by construction, no eviction job needed.

Revision ID: 015
Revises: 014
Create Date: 2026-07-01 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector


revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── memory_items : distilled, embedded, derived event index ──────────────
    op.create_table(
        "memory_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        # Stable identity for consolidation/dedup (derived from the news cluster).
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("sentiment", sa.String(10), nullable=True),   # positive|negative|neutral
        sa.Column("magnitude", sa.Integer(), nullable=True),    # 1..5
        sa.Column("time_horizon", sa.String(20), nullable=True),
        sa.Column("macro_theme", sa.String(100), nullable=True),
        # Companies / sectors this event touched (direct impacts), for scope filters.
        sa.Column("symbols", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("sectors", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("trigger_ids", postgresql.ARRAY(sa.Text()), nullable=True),
        # Anchored on the event's real time (earlier of publish-time and analysis
        # time), so backfilled/delayed items don't look artificially fresh.
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0"),
        # Provenance: which news/impact rows this consolidated event came from.
        sa.Column("source_news_ids", postgresql.JSONB(), nullable=True),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("event_key", name="uq_memory_items_event_key"),
    )
    op.create_index("ix_memory_items_event_time", "memory_items", ["event_time"])
    op.create_index("ix_memory_items_macro_theme", "memory_items", ["macro_theme"])
    # GIN indexes for fast array-membership scope filters (symbols && [..], sectors && [..]).
    op.execute("CREATE INDEX ix_memory_items_symbols ON memory_items USING gin (symbols)")
    op.execute("CREATE INDEX ix_memory_items_sectors ON memory_items USING gin (sectors)")
    # HNSW cosine index for semantic relevance retrieval (matches companies.embedding pattern).
    op.execute(
        "CREATE INDEX ix_memory_items_embedding ON memory_items "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    # ── memory_digests : overwrite cache for sector/market narrative ─────────
    op.create_table(
        "memory_digests",
        sa.Column("scope_type", sa.String(10), nullable=False),   # 'sector' | 'market'
        sa.Column("scope_key", sa.String(120), nullable=False),   # sector code | 'MARKET'
        sa.Column("digest_text", sa.Text(), nullable=True),
        sa.Column("net_tilt", sa.String(30), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_window_days", sa.Integer(), nullable=True),
        # Hash of the aggregate the digest was built from — skip regen when unchanged.
        sa.Column("input_hash", sa.Text(), nullable=True),
        sa.Column("model_used", sa.String(80), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("scope_type", "scope_key", name="pk_memory_digests"),
    )


def downgrade() -> None:
    op.drop_table("memory_digests")
    op.drop_index("ix_memory_items_embedding", table_name="memory_items")
    op.drop_index("ix_memory_items_sectors", table_name="memory_items")
    op.drop_index("ix_memory_items_symbols", table_name="memory_items")
    op.drop_index("ix_memory_items_macro_theme", table_name="memory_items")
    op.drop_index("ix_memory_items_event_time", table_name="memory_items")
    op.drop_table("memory_items")

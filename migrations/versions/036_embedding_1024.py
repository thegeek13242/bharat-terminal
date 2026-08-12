"""Additive 1024-dim embedding columns for the Bedrock embedding migration.

The AWS host has no GCP ADC, so Vertex text-embedding-004 (768-dim) is dead
there; the replacement (Cohere Embed v4 on Bedrock) emits 1024 dims — no
Bedrock model emits 768. Per AWS_MIGRATION_PLAN_2026-08-08.md §C this is
additive: new column + index alongside the old, readers swap via
EMBEDDING_COLUMN (llm/embeddings.py), old `embedding` columns dropped in a
LATER migration once the swap is proven.

companies.embedding is untouched — it has never held a vector (0 embedded
rows, re-verified 2026-08-12).

Revision ID: 036
Revises: 035
Create Date: 2026-08-12 00:00:00.000000
"""
from alembic import op


revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE memory_items ADD COLUMN embedding_1024 vector(1024)")
    op.execute("ALTER TABLE transcript_chunks ADD COLUMN embedding_1024 vector(1024)")
    # HNSW builds incrementally as the backfill fills the columns; at the
    # current scale (~13k + ~23k rows) build cost is negligible either way.
    op.execute(
        "CREATE INDEX ix_memory_items_embedding_1024 ON memory_items "
        "USING hnsw (embedding_1024 vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_transcript_chunks_embedding_1024 ON transcript_chunks "
        "USING hnsw (embedding_1024 vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_transcript_chunks_embedding_1024")
    op.execute("DROP INDEX IF EXISTS ix_memory_items_embedding_1024")
    op.execute("ALTER TABLE transcript_chunks DROP COLUMN IF EXISTS embedding_1024")
    op.execute("ALTER TABLE memory_items DROP COLUMN IF EXISTS embedding_1024")

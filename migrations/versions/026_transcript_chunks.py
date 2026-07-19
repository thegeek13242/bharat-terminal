"""Transcript chunk embeddings — semantic retrieval substrate (A1 phase 3.2).

One row per chunk: a contiguous (char_start, char_end) SPAN of the
transcript's full_text — the verbatim doctrine extends to retrieval: what
search returns is always a provable slice of the filed document. Chunks are
segment-aligned (never split a speaker turn mid-thought except turns beyond
the hard cap) and never cross the prepared-remarks/Q&A boundary.

Embeddings: 768-dim text-embedding-004, HNSW cosine — the exact pattern of
`memory_items` (see llm/embeddings.py; dim pinned there).

Revision ID: 026
Revises: 025
Create Date: 2026-07-19 00:00:00.000000
"""
from alembic import op


revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE transcript_chunks (
            id                serial PRIMARY KEY,
            transcript_id     integer NOT NULL,      -- concall_transcripts.id
            symbol            varchar(20) NOT NULL,
            announced_at      timestamptz NOT NULL,
            chunk_index       integer NOT NULL,
            char_start        integer NOT NULL,
            char_end          integer NOT NULL,
            section           varchar(12),           -- prepared | qa | null
            embedding         vector(768) NOT NULL,
            embedding_version integer NOT NULL,
            created_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_transcript_chunk UNIQUE (transcript_id, chunk_index)
        )
    """)
    op.execute(
        "CREATE INDEX ix_transcript_chunks_embedding ON transcript_chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        "CREATE INDEX ix_transcript_chunks_symbol "
        "ON transcript_chunks (symbol, announced_at DESC)"
    )
    # Embed marker on the transcript (same rationale as guidance_version).
    op.execute(
        "ALTER TABLE concall_transcripts ADD COLUMN embed_version integer"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS transcript_chunks")
    op.execute(
        "ALTER TABLE concall_transcripts DROP COLUMN IF EXISTS embed_version"
    )

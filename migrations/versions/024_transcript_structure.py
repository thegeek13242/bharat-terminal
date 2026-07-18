"""Transcript structure annotations (A1 phase 2 — AlphaSense layer).

`structure` is an ANNOTATION layer over the verbatim `full_text`: speaker
segments with roles, category highlights, Q&A boundary, and page offsets —
all as (start, end) character offsets anchored by exact string match into the
stored text. The base transcript is never rewritten; annotations that fail
verbatim anchoring are dropped at build time, never guessed.

Revision ID: 024
Revises: 023
Create Date: 2026-07-19 00:00:00.000000
"""
from alembic import op


revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE concall_transcripts
            ADD COLUMN structure        jsonb,
            ADD COLUMN structure_status varchar(16) NOT NULL DEFAULT 'pending',
            ADD COLUMN structure_model  varchar(64),
            ADD COLUMN structured_at    timestamptz
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE concall_transcripts
            DROP COLUMN IF EXISTS structure,
            DROP COLUMN IF EXISTS structure_status,
            DROP COLUMN IF EXISTS structure_model,
            DROP COLUMN IF EXISTS structured_at
    """)

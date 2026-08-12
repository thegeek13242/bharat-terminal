"""Drop NOT NULL on transcript_chunks.embedding (the retired 768-dim column).

Migration 036 moved the active vector to embedding_1024; fresh chunk inserts
write ONLY the active column, but the original schema declared the 768 column
NOT NULL — so every new transcript embedded after the swap failed with a
NotNullViolation (first seen 2026-08-13 on the Aug 9-11 window recovery;
the bulk backfill masked it by UPDATE-ing existing rows). The old column is
kept for rollback/verification until its removal migration; it can no longer
be populated (Vertex is unreachable from this host), so it must be nullable.

Revision ID: 037
Revises: 036
Create Date: 2026-08-13 00:00:00.000000
"""
from alembic import op


revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE transcript_chunks ALTER COLUMN embedding DROP NOT NULL")


def downgrade() -> None:
    # Rows inserted after 036 have NULL embedding; restoring NOT NULL would
    # fail on them — downgrade is intentionally a no-op.
    pass

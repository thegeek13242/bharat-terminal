"""Discussed-quarter label on transcripts (Quarter filter).

period_label ('Q1FY27') = the quarter the call DISCUSSES, derived
deterministically from the transcript's own title text (~90% name it) with
an earnings-season date fallback — filing date alone misfiles stragglers
(Q4 calls filed in July). See transcript_sync.derive_period.

Revision ID: 029
Revises: 028
Create Date: 2026-07-19 00:00:00.000000
"""
from alembic import op


revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE concall_transcripts ADD COLUMN period_label varchar(8)"
    )
    op.execute(
        "CREATE INDEX ix_concall_transcripts_period "
        "ON concall_transcripts (period_label)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE concall_transcripts DROP COLUMN IF EXISTS period_label"
    )

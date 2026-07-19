"""Structured guidance extracted from concall transcripts (A1 phase 3).

One row per forward-looking management guidance item. `quote_start/end` are
character offsets into the transcript's `full_text` — the verbatim-anchoring
doctrine: an item exists only if its quote is provably in the transcript.
Values are stored AS STATED (no unit conversion — "6.5 lakh tons" is
value_low=6.5 unit='lakh tons'); QoQ verdicts are computed arithmetically
only when unit+period match the paired prior item, otherwise 'changed'.

`prior_guidance_id` is a logical link (repo style: no FK) to the matched
item from the company's previous call. Re-extraction deletes+reinserts a
transcript's rows; successors of a re-extracted prior should be re-run too.

Revision ID: 025
Revises: 024
Create Date: 2026-07-19 00:00:00.000000
"""
from alembic import op


revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE transcript_guidance (
            id                 serial PRIMARY KEY,
            transcript_id      integer NOT NULL,     -- concall_transcripts.id
            symbol             varchar(20) NOT NULL,
            announced_at       timestamptz NOT NULL,
            metric             text NOT NULL,        -- e.g. 'EBITDA margin'
            period             text,                 -- e.g. 'FY27', 'Q2 FY27'
            value_low          double precision,
            value_high         double precision,
            unit               text,                 -- as stated: '%', 'lakh tons'
            quote_start        integer NOT NULL,
            quote_end          integer NOT NULL,
            prior_guidance_id  integer,              -- matched item, previous call
            change_verdict     varchar(12) NOT NULL, -- new|raised|lowered|maintained|changed
            extraction_version integer NOT NULL,
            created_at         timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX ix_transcript_guidance_symbol "
        "ON transcript_guidance (symbol, announced_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_transcript_guidance_transcript "
        "ON transcript_guidance (transcript_id)"
    )
    # Extraction marker on the transcript itself: a call that yields ZERO
    # guidance items is a valid outcome and must not re-qualify for
    # extraction forever (item rows alone can't record "extracted, empty").
    op.execute(
        "ALTER TABLE concall_transcripts ADD COLUMN guidance_version integer"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS transcript_guidance")
    op.execute(
        "ALTER TABLE concall_transcripts DROP COLUMN IF EXISTS guidance_version"
    )

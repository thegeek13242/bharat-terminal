"""Concall transcripts — complete earnings-call transcript text (A1).

One row per transcript filing (news_items announcement -> nsearchives PDF).
`full_text` is the COMPLETE extracted transcript — no page cap, no char cap
(founder rule 2026-07-19: never clip the transcript). Dedup key is the source
announcement id, so re-runs are idempotent and a re-filed/corrected transcript
under a new announcement id becomes a new row.

Revision ID: 023
Revises: 022
Create Date: 2026-07-19 00:00:00.000000
"""
from alembic import op


revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE concall_transcripts (
            id            serial PRIMARY KEY,
            symbol        varchar(20) NOT NULL,        -- resolved exchange symbol
            news_id       text NOT NULL,               -- source news_items.id (dedupe)
            source        varchar(16) NOT NULL,        -- NSE_FILINGS | BSE_FILINGS
            headline      text,
            announced_at  timestamptz NOT NULL,        -- announcement timestamp_utc
            pdf_url       text NOT NULL,
            pdf_bytes     integer,
            page_count    integer,
            char_count    integer,
            full_text     text,                        -- COMPLETE transcript, never clipped
            parse_status  varchar(24) NOT NULL,        -- ok | fetch_failed | extract_failed | not_pdf
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_concall_transcript_news UNIQUE (news_id)
        )
    """)
    op.execute(
        "CREATE INDEX ix_concall_transcripts_symbol_announced "
        "ON concall_transcripts (symbol, announced_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_concall_transcripts_announced "
        "ON concall_transcripts (announced_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS concall_transcripts")

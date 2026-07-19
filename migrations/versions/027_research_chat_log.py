"""Research-chat audit log (F12) — append-only, one row per answer.

Every Q&A the chat produces is recorded with its full tool trace and
citations — the analysis_audit doctrine applied to conversational research:
an answer either has an auditable evidence trail or it should not exist.
No update path; rows are never mutated.

Revision ID: 027
Revises: 026
Create Date: 2026-07-19 00:00:00.000000
"""
from alembic import op


revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE research_chat_log (
            id              serial PRIMARY KEY,
            user_email      text NOT NULL,
            asked_at        timestamptz NOT NULL DEFAULT now(),
            question        text NOT NULL,
            history_len     integer NOT NULL DEFAULT 0,
            answer          text,
            status          varchar(16) NOT NULL,   -- ok | model_error | empty
            citations       jsonb,
            tool_trace      jsonb,                  -- [{ref, tool, args, summary}]
            model           varchar(64),
            latency_ms      double precision,
            turns           integer,
            input_tokens    integer,
            output_tokens   integer,
            thinking_tokens integer,
            web_search_calls integer
        )
    """)
    op.execute(
        "CREATE INDEX ix_research_chat_log_asked "
        "ON research_chat_log (asked_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS research_chat_log")

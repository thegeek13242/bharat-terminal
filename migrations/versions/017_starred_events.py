"""Account-level starred events (feed shortlist).

One row per (user, impact report) star. Replaces the interim per-browser
localStorage implementation so stars follow the account across devices.
Full report payloads are NOT copied here — the GET endpoint joins back to
impact_reports/news_items, which are permanent, and serialization goes
through the same path that strips trade_signals (SEBI RA scope).

Revision ID: 017
Revises: 016
Create Date: 2026-07-02 00:00:00.000000
"""
from alembic import op


revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE starred_events (
            user_id          bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
            impact_report_id text   NOT NULL REFERENCES impact_reports(id) ON DELETE CASCADE,
            starred_at       timestamp with time zone NOT NULL DEFAULT now(),
            CONSTRAINT pk_starred_events PRIMARY KEY (user_id, impact_report_id)
        )
    """)
    op.execute(
        "CREATE INDEX ix_starred_user_time ON starred_events (user_id, starred_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS starred_events CASCADE")

"""Add signal_reports table — per-trading-day actionable signal report

Signals generated after the 15:30 IST market close are actionable on the NEXT
trading day. This table holds one comprehensive report per trading day D: every
trade signal generated in the window (close(P), close(D)] — where P is the
previous trading day and close(x) = x at 15:30 IST — joined back to its source
news, impact analysis and company context, plus day-level summary stats.

The trading calendar is derived from price_points (actual NSE bhavcopy dates, so
weekends AND holidays are already excluded). Reports are regenerated idempotently
(upsert on trading_day): intra-day the report grows as more signals land; once
the session is over it is stable. generated_at records the last build time.

Revision ID: 014
Revises: 013
Create Date: 2026-06-28 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_reports",
        # The trading day the signals are actionable on (NSE session date).
        sa.Column("trading_day", sa.Date(), primary_key=True),
        sa.Column("prev_trading_day", sa.Date(), nullable=True),
        # Generation window: signals with created_at in (window_start, window_end].
        sa.Column("window_start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),

        # ── day-level summary ─────────────────────────────────────────────
        sa.Column("n_signals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("n_long", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("n_short", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("n_high_conviction", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("distinct_symbols", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("distinct_sources", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_relevance_confidence", sa.Float(), nullable=True),
        # breakdowns by conviction / instrument / sector / macro_theme
        sa.Column("summary", postgresql.JSONB(), nullable=True),

        # ── comprehensive per-signal detail (array of objects) ────────────
        sa.Column("signals", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_signal_reports_generated_at", "signal_reports", ["generated_at"])


def downgrade() -> None:
    op.drop_index("ix_signal_reports_generated_at", table_name="signal_reports")
    op.drop_table("signal_reports")

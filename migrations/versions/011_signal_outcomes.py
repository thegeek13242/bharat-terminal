"""Add signal_outcomes table (Feature 2 — signal accuracy tracking)

Signals are stored anonymously inside impact_reports.trade_signals (JSONB). This
table gives each one a stable synthetic id — sha256(impact_report_id|symbol|
direction)[:32] — so realized forward-return outcomes can be tracked and sliced
without rewriting any history or touching the emission path. Populated +
evaluated idempotently by research/score_signals.py.

Revision ID: 011
Revises: 010
Create Date: 2026-06-26 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_outcomes",
        sa.Column("signal_id", sa.Text(), primary_key=True),
        sa.Column("impact_report_id", sa.Text(), nullable=False),
        sa.Column("news_id", sa.Text(), nullable=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("conviction", sa.String(length=10), nullable=True),
        sa.Column("instrument_type", sa.String(length=20), nullable=True),
        sa.Column("macro_theme", sa.String(length=100), nullable=True),
        sa.Column("sector", sa.String(length=100), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("trigger_id", sa.String(length=50), nullable=True),
        sa.Column("signal_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=True),
        sa.Column("ret_1d", sa.Float(), nullable=True),
        sa.Column("ret_5d", sa.Float(), nullable=True),
        sa.Column("ret_10d", sa.Float(), nullable=True),
        sa.Column("ret_20d", sa.Float(), nullable=True),
        sa.Column("correct_1d", sa.Boolean(), nullable=True),
        sa.Column("correct_5d", sa.Boolean(), nullable=True),
        sa.Column("correct_10d", sa.Boolean(), nullable=True),
        sa.Column("correct_20d", sa.Boolean(), nullable=True),
        sa.Column("implied_move_pct", sa.Float(), nullable=True),
        sa.Column("eval_status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_category", sa.String(length=40), nullable=True),
        sa.Column("failure_rationale", sa.Text(), nullable=True),
        sa.Column("postmortem_model", sa.String(length=60), nullable=True),
        sa.Column("postmortem_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_signal_outcomes_impact_report_id", "signal_outcomes", ["impact_report_id"])
    op.create_index("ix_signal_outcomes_symbol", "signal_outcomes", ["symbol"])
    op.create_index("ix_signal_outcomes_conviction", "signal_outcomes", ["conviction"])
    op.create_index("ix_signal_outcomes_macro_theme", "signal_outcomes", ["macro_theme"])
    op.create_index("ix_signal_outcomes_source", "signal_outcomes", ["source"])
    op.create_index("ix_signal_outcomes_eval_status", "signal_outcomes", ["eval_status"])
    op.create_index("ix_signal_outcomes_created", "signal_outcomes", ["signal_created_at"])


def downgrade() -> None:
    op.drop_index("ix_signal_outcomes_created", table_name="signal_outcomes")
    op.drop_index("ix_signal_outcomes_eval_status", table_name="signal_outcomes")
    op.drop_index("ix_signal_outcomes_source", table_name="signal_outcomes")
    op.drop_index("ix_signal_outcomes_macro_theme", table_name="signal_outcomes")
    op.drop_index("ix_signal_outcomes_conviction", table_name="signal_outcomes")
    op.drop_index("ix_signal_outcomes_symbol", table_name="signal_outcomes")
    op.drop_index("ix_signal_outcomes_impact_report_id", table_name="signal_outcomes")
    op.drop_table("signal_outcomes")

"""Add result_events table for event-driven financial sync

Revision ID: 005
Revises: 004
"""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "result_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(20), sa.ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False),
        sa.Column("result_date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sync_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("symbol", "result_date", name="uq_result_event"),
    )
    op.create_index("ix_result_events_date", "result_events", ["result_date"])
    op.create_index("ix_result_events_status", "result_events", ["sync_status"])

    # Also fix price_points.volume to BIGINT if not already done
    op.alter_column("price_points", "volume", type_=sa.BigInteger())


def downgrade():
    op.alter_column("price_points", "volume", type_=sa.Integer())
    op.drop_index("ix_result_events_status", table_name="result_events")
    op.drop_index("ix_result_events_date", table_name="result_events")
    op.drop_table("result_events")

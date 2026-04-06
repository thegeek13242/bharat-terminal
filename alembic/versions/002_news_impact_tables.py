"""Add news_items and impact_reports tables

Revision ID: 002
Revises: 001
Create Date: 2026-04-06 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'news_items',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('source', sa.String(50), nullable=False),
        sa.Column('timestamp_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('headline', sa.Text(), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('ingest_latency_ms', sa.Float(), nullable=False),
        sa.Column('category', sa.String(50), nullable=True),
        sa.Column('symbols_mentioned', postgresql.JSONB(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_news_items_source', 'news_items', ['source'])
    op.create_index('ix_news_items_timestamp', 'news_items', ['timestamp_utc'])
    op.create_index('ix_news_items_source_ts', 'news_items', ['source', 'timestamp_utc'])

    op.create_table(
        'impact_reports',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('news_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('relevant', sa.Boolean(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('macro_theme', sa.String(100), nullable=True),
        sa.Column('affected_sectors', postgresql.JSONB(), nullable=True),
        sa.Column('company_impacts', postgresql.JSONB(), nullable=True),
        sa.Column('trade_signals', postgresql.JSONB(), nullable=True),
        sa.Column('processing_latency_ms', sa.Float(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_impact_reports_news_id', 'impact_reports', ['news_id'])
    op.create_index('ix_impact_reports_relevant', 'impact_reports', ['relevant'])
    op.create_index('ix_impact_reports_created_at', 'impact_reports', ['created_at'])
    op.create_index(
        'ix_impact_reports_created_relevant',
        'impact_reports',
        ['created_at', 'relevant'],
    )


def downgrade() -> None:
    op.drop_table('impact_reports')
    op.drop_table('news_items')

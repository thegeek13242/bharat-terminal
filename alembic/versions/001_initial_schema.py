"""Initial schema: companies, relationships, price_points

Revision ID: 001
Revises:
Create Date: 2025-01-01 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        'companies',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('isin', sa.String(12), nullable=True),
        sa.Column('exchange', sa.String(5), nullable=False),
        sa.Column('company_name', sa.String(200), nullable=False),
        sa.Column('aliases', postgresql.JSONB(), nullable=True),
        sa.Column('sector_nse', sa.String(100), nullable=True),
        sa.Column('industry_nse', sa.String(100), nullable=True),
        sa.Column('bse_group', sa.String(5), nullable=True),
        sa.Column('mcap_cr', sa.Float(), nullable=True),
        sa.Column('listing_date', sa.Date(), nullable=True),
        sa.Column('description_200w', sa.Text(), nullable=True),
        sa.Column('revenue_segments', postgresql.JSONB(), nullable=True),
        sa.Column('geography_split', postgresql.JSONB(), nullable=True),
        sa.Column('moat_classification', sa.String(50), nullable=True),
        sa.Column('revenue_ttm_cr', sa.Float(), nullable=True),
        sa.Column('ebitda_margin_pct', sa.Float(), nullable=True),
        sa.Column('pat_cr', sa.Float(), nullable=True),
        sa.Column('eps_ttm', sa.Float(), nullable=True),
        sa.Column('pe_ratio', sa.Float(), nullable=True),
        sa.Column('pb_ratio', sa.Float(), nullable=True),
        sa.Column('roe_pct', sa.Float(), nullable=True),
        sa.Column('net_debt_cr', sa.Float(), nullable=True),
        sa.Column('interest_coverage', sa.Float(), nullable=True),
        sa.Column('fcf_yield_pct', sa.Float(), nullable=True),
        sa.Column('financials_updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('wacc_pct', sa.Float(), nullable=True),
        sa.Column('terminal_growth_pct', sa.Float(), nullable=True),
        sa.Column('projection_years', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('year_projections', postgresql.JSONB(), nullable=True),
        sa.Column('fair_value_per_share', sa.Float(), nullable=True),
        sa.Column('margin_of_safety_pct', sa.Float(), nullable=True),
        sa.Column('dcf_bull_value', sa.Float(), nullable=True),
        sa.Column('dcf_bear_value', sa.Float(), nullable=True),
        sa.Column('dcf_updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dcf_source', sa.String(50), nullable=True),
        sa.Column('dcf_confidence', sa.Float(), nullable=True),
        sa.Column('median_target_price', sa.Float(), nullable=True),
        sa.Column('buy_pct', sa.Float(), nullable=True),
        sa.Column('hold_pct', sa.Float(), nullable=True),
        sa.Column('sell_pct', sa.Float(), nullable=True),
        sa.Column('num_analysts', sa.Integer(), nullable=True),
        sa.Column('eps_fy_curr', sa.Float(), nullable=True),
        sa.Column('eps_fy_next', sa.Float(), nullable=True),
        sa.Column('consensus_updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('embedding', Vector(768), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('data_quality_score', sa.Float(), nullable=False, server_default='0.0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('symbol', name='uq_companies_symbol'),
        sa.UniqueConstraint('isin', name='uq_companies_isin'),
    )

    op.create_index('ix_companies_symbol', 'companies', ['symbol'])
    op.create_index('ix_companies_isin', 'companies', ['isin'])
    op.create_index('ix_companies_sector', 'companies', ['sector_nse'])
    op.create_index('ix_companies_mcap', 'companies', ['mcap_cr'])
    op.create_index('ix_companies_exchange', 'companies', ['exchange'])

    # pgvector HNSW index for fast similarity search
    op.execute("""
        CREATE INDEX ix_companies_embedding ON companies
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # Full text search index on company name using trigram similarity
    op.execute("""
        CREATE INDEX ix_companies_name_trgm ON companies
        USING gin (company_name gin_trgm_ops)
    """)

    op.create_table(
        'company_relationships',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('source_symbol', sa.String(20), sa.ForeignKey('companies.symbol', ondelete='CASCADE'), nullable=False),
        sa.Column('target_symbol', sa.String(20), nullable=False),
        sa.Column('target_name', sa.String(200), nullable=True),
        sa.Column('relationship_type', sa.String(20), nullable=False),
        sa.Column('weight', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('source_of_truth', sa.String(50), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='0.8'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source_symbol', 'target_symbol', 'relationship_type', name='uq_relationship'),
    )

    op.create_index('ix_relationships_source', 'company_relationships', ['source_symbol'])
    op.create_index('ix_relationships_target', 'company_relationships', ['target_symbol'])

    op.create_table(
        'price_points',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('symbol', sa.String(20), sa.ForeignKey('companies.symbol', ondelete='CASCADE'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('open', sa.Float(), nullable=True),
        sa.Column('high', sa.Float(), nullable=True),
        sa.Column('low', sa.Float(), nullable=True),
        sa.Column('close', sa.Float(), nullable=False),
        sa.Column('volume', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('symbol', 'date', name='uq_price_point'),
    )

    op.create_index('ix_prices_symbol_date', 'price_points', ['symbol', 'date'])


def downgrade() -> None:
    op.drop_table('price_points')
    op.drop_table('company_relationships')
    op.drop_table('companies')
    op.execute("DROP EXTENSION IF EXISTS vector")

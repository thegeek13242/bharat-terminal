"""Add financial_results, nse_filings, shareholding_patterns, corporate_actions, sync_log

These tables store structured financial data ingested from NSE XBRL filings
(primary source) and Screener.in (secondary verification). They replace the
hardcoded seed data with real, auditable, machine-readable financial records.

Revision ID: 004
Revises: 003
Create Date: 2026-04-12 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── NSE Filing Index ─────────────────────────────────────────────────────
    # Stores the metadata from NSE's corporates-financial-results API.
    # One row per filing (each quarter has consolidated + standalone = 2 rows).
    # Keeps XBRL URLs so we never re-fetch the filing index unnecessarily.
    op.create_table(
        'nse_filings',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('isin', sa.String(12), nullable=True),
        sa.Column('from_date', sa.Date(), nullable=False),
        sa.Column('to_date', sa.Date(), nullable=False),
        sa.Column('period', sa.String(20), nullable=False),           # Quarterly, Annual, Half-Yearly
        sa.Column('relating_to', sa.String(50), nullable=True),       # First Half, etc.
        sa.Column('financial_year', sa.String(40), nullable=True),
        sa.Column('consolidated', sa.String(30), nullable=False),     # Consolidated, Non-Consolidated
        sa.Column('audited', sa.String(20), nullable=False),          # Audited, Un-Audited
        sa.Column('ind_as', sa.String(30), nullable=True),            # Ind-AS New, Non-Ind-AS
        sa.Column('format', sa.String(10), nullable=True),            # New, Old
        sa.Column('xbrl_url', sa.Text(), nullable=True),
        sa.Column('detail_url', sa.Text(), nullable=True),
        sa.Column('seq_number', sa.String(20), nullable=True),
        sa.Column('filing_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('broadcast_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('xbrl_downloaded', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('xbrl_parsed', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('symbol', 'from_date', 'to_date', 'consolidated', name='uq_nse_filing'),
    )
    op.create_index('ix_nse_filings_symbol', 'nse_filings', ['symbol'])
    op.create_index('ix_nse_filings_to_date', 'nse_filings', ['to_date'])
    op.create_index('ix_nse_filings_parsed', 'nse_filings', ['xbrl_parsed'])

    # ── Financial Results ────────────────────────────────────────────────────
    # Quarterly/annual P&L data parsed from XBRL. One row per company per period.
    # Stores both quarterly and cumulative (YTD) figures from the XBRL.
    op.create_table(
        'financial_results',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('symbol', sa.String(20), sa.ForeignKey('companies.symbol', ondelete='CASCADE'), nullable=False),
        sa.Column('period_end', sa.Date(), nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('period_type', sa.String(20), nullable=False),      # quarterly, annual, half_yearly
        sa.Column('consolidated', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('audited', sa.Boolean(), nullable=False, server_default='false'),
        # P&L line items (all in Crores INR)
        sa.Column('revenue_cr', sa.Float(), nullable=True),
        sa.Column('other_income_cr', sa.Float(), nullable=True),
        sa.Column('total_income_cr', sa.Float(), nullable=True),
        sa.Column('cost_of_materials_cr', sa.Float(), nullable=True),
        sa.Column('purchases_stock_in_trade_cr', sa.Float(), nullable=True),
        sa.Column('inventory_changes_cr', sa.Float(), nullable=True),
        sa.Column('employee_expense_cr', sa.Float(), nullable=True),
        sa.Column('finance_costs_cr', sa.Float(), nullable=True),
        sa.Column('depreciation_cr', sa.Float(), nullable=True),
        sa.Column('other_expenses_cr', sa.Float(), nullable=True),
        sa.Column('total_expenses_cr', sa.Float(), nullable=True),
        sa.Column('pbt_before_exceptional_cr', sa.Float(), nullable=True),
        sa.Column('exceptional_items_cr', sa.Float(), nullable=True),
        sa.Column('pbt_cr', sa.Float(), nullable=True),
        sa.Column('tax_cr', sa.Float(), nullable=True),
        sa.Column('pat_cr', sa.Float(), nullable=True),
        sa.Column('pat_attributable_cr', sa.Float(), nullable=True),   # Attributable to parent owners
        sa.Column('other_comprehensive_income_cr', sa.Float(), nullable=True),
        # EPS
        sa.Column('eps_basic', sa.Float(), nullable=True),
        sa.Column('eps_diluted', sa.Float(), nullable=True),
        sa.Column('face_value', sa.Float(), nullable=True),
        sa.Column('paid_up_capital_cr', sa.Float(), nullable=True),
        # Ratios from XBRL
        sa.Column('debt_equity_ratio', sa.Float(), nullable=True),
        sa.Column('interest_coverage_ratio', sa.Float(), nullable=True),
        # Derived (computed after parse)
        sa.Column('ebitda_cr', sa.Float(), nullable=True),
        sa.Column('ebitda_margin_pct', sa.Float(), nullable=True),
        # Segment data (JSONB for flexibility — different companies have different segments)
        sa.Column('segment_data', postgresql.JSONB(), nullable=True),
        # YTD cumulative figures (from FourD context in XBRL)
        sa.Column('ytd_revenue_cr', sa.Float(), nullable=True),
        sa.Column('ytd_pat_cr', sa.Float(), nullable=True),
        sa.Column('ytd_eps_basic', sa.Float(), nullable=True),
        # Provenance
        sa.Column('source', sa.String(20), nullable=False, server_default='nse_xbrl'),
        sa.Column('nse_filing_id', sa.Integer(), sa.ForeignKey('nse_filings.id'), nullable=True),
        sa.Column('raw_xbrl_facts', postgresql.JSONB(), nullable=True),   # Full fact dump for audit
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('symbol', 'period_end', 'period_type', 'consolidated', name='uq_financial_result'),
    )
    op.create_index('ix_financial_results_symbol', 'financial_results', ['symbol'])
    op.create_index('ix_financial_results_period', 'financial_results', ['period_end'])
    op.create_index('ix_financial_results_symbol_period', 'financial_results', ['symbol', 'period_end'])

    # ── Shareholding Patterns ────────────────────────────────────────────────
    op.create_table(
        'shareholding_patterns',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('symbol', sa.String(20), sa.ForeignKey('companies.symbol', ondelete='CASCADE'), nullable=False),
        sa.Column('quarter_end', sa.Date(), nullable=False),
        sa.Column('promoter_pct', sa.Float(), nullable=True),
        sa.Column('promoter_pledge_pct', sa.Float(), nullable=True),
        sa.Column('fii_pct', sa.Float(), nullable=True),
        sa.Column('dii_pct', sa.Float(), nullable=True),
        sa.Column('public_pct', sa.Float(), nullable=True),
        sa.Column('num_shareholders', sa.Integer(), nullable=True),
        sa.Column('source', sa.String(20), nullable=False, server_default='nse'),
        sa.Column('raw_data', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('symbol', 'quarter_end', name='uq_shareholding'),
    )
    op.create_index('ix_shareholding_symbol', 'shareholding_patterns', ['symbol'])
    op.create_index('ix_shareholding_quarter', 'shareholding_patterns', ['quarter_end'])

    # ── Corporate Actions ────────────────────────────────────────────────────
    op.create_table(
        'corporate_actions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('symbol', sa.String(20), sa.ForeignKey('companies.symbol', ondelete='CASCADE'), nullable=False),
        sa.Column('ex_date', sa.Date(), nullable=True),
        sa.Column('record_date', sa.Date(), nullable=True),
        sa.Column('action_type', sa.String(20), nullable=False),       # DIVIDEND, SPLIT, BONUS, RIGHTS, BUYBACK
        sa.Column('details', postgresql.JSONB(), nullable=True),
        sa.Column('value', sa.Float(), nullable=True),                 # Dividend amount, split ratio, etc.
        sa.Column('source', sa.String(20), nullable=False, server_default='nse'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('symbol', 'ex_date', 'action_type', name='uq_corporate_action'),
    )
    op.create_index('ix_corp_actions_symbol', 'corporate_actions', ['symbol'])
    op.create_index('ix_corp_actions_date', 'corporate_actions', ['ex_date'])

    # ── Sync Log ─────────────────────────────────────────────────────────────
    op.create_table(
        'sync_log',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('sync_type', sa.String(50), nullable=False),         # company_master, filing_index, xbrl_parse, financials, prices, screener_verify
        sa.Column('source', sa.String(30), nullable=False),            # nse, bse, screener
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='running'),  # running, success, failed
        sa.Column('records_processed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('records_failed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('metadata_json', postgresql.JSONB(), nullable=True),    # last_symbol_processed, etc.
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sync_log_type', 'sync_log', ['sync_type'])
    op.create_index('ix_sync_log_status', 'sync_log', ['status'])


def downgrade() -> None:
    op.drop_table('sync_log')
    op.drop_table('corporate_actions')
    op.drop_table('shareholding_patterns')
    op.drop_table('financial_results')
    op.drop_table('nse_filings')

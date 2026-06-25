"""Add taxonomy + sector_financials to financial_results

Companies file financial results under DIFFERENT XBRL taxonomies by category
(general Ind AS, NBFC, banking, life/general insurance, non-Ind AS). Their P&L
line items genuinely differ — a bank reports interest_earned / interest_expended
/ provisions; an insurer reports premiums / claims_incurred / net_commission —
none of which fit the generic Ind AS columns. Forcing every company into one
column set is lossy.

This migration adds two nullable columns (purely additive — existing rows and
queries are unaffected):
  • taxonomy          — the accounting taxonomy the row was filed under, so
                        consumers know how to interpret the common columns.
  • sector_financials — JSONB of normalized sector-specific P&L lines, populated
                        only for banking / insurance (NULL for Ind AS / NBFC /
                        non-Ind AS, whose lines map to the typed columns).

Revision ID: 010
Revises: 009
Create Date: 2026-06-25 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "financial_results",
        sa.Column("taxonomy", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "financial_results",
        sa.Column("sector_financials", JSONB(), nullable=True),
    )
    op.create_index(
        "ix_financial_results_taxonomy", "financial_results", ["taxonomy"]
    )


def downgrade() -> None:
    op.drop_index("ix_financial_results_taxonomy", table_name="financial_results")
    op.drop_column("financial_results", "sector_financials")
    op.drop_column("financial_results", "taxonomy")

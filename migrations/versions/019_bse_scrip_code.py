"""BSE scrip code on companies (BSE-only universe onboarding).

Adds `bse_scrip_code` — the 6-digit BSE security code used by all
api.bseindia.com endpoints. Populated for BSE-only companies at seed time
and backfilled (by ISIN match) for dual-listed rows. Uniqueness is a
partial index so the existing all-NULL NSE rows are untouched.

Revision ID: 019
Revises: 018
Create Date: 2026-07-03 00:00:00.000000
"""
from alembic import op


revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE companies ADD COLUMN bse_scrip_code VARCHAR(10)")
    op.execute(
        "CREATE UNIQUE INDEX ix_companies_bse_scrip_code "
        "ON companies (bse_scrip_code) WHERE bse_scrip_code IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_companies_bse_scrip_code")
    op.execute("ALTER TABLE companies DROP COLUMN IF EXISTS bse_scrip_code")

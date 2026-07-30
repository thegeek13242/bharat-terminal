"""Company-type axis + market-state axis (Analyst Clone Phase 1, item #0).

company_type: the PRIMARY accounting-regime axis — what the P&L means —
from the measured detectors in ANALYST_CLONE_ARCHITECTURE_2026-07-29.md §2
(bank 41/42 precision, insurers 11/11, holdco FY-dividend-share rule 12/12
incl. the MAHSCOOTER class whose absence caused a live mis-shorted trade).
type_overlays: non-exclusive modifiers (psu, distress_attached, suspended,
circuit_prone, loss_making, loss_making_growth, recently_listed,
cic_declared). is_fund_scheme: universe hygiene — 243 "companies" are
mutual-fund scheme listings that must never route to analysis as equities.

market_state: daily ASM/GSM/ESM surveillance stages + price band per
symbol from NSE's published lists. GSM-III trades weekly and GSM-IV bans
upward moves — filings on those names cannot show normal reactions, so
this table is a label-validity and execution gate, not decoration.

Revision ID: 033
Revises: 032
Create Date: 2026-07-29 00:00:00.000000
"""
from alembic import op


revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE companies ADD COLUMN company_type VARCHAR(20)")
    op.execute("ALTER TABLE companies ADD COLUMN type_overlays JSONB "
               "NOT NULL DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE companies ADD COLUMN type_confidence "
               "DOUBLE PRECISION")
    op.execute("ALTER TABLE companies ADD COLUMN type_detected_at TIMESTAMPTZ")
    op.execute("ALTER TABLE companies ADD COLUMN is_fund_scheme BOOLEAN "
               "NOT NULL DEFAULT FALSE")
    op.execute("CREATE INDEX ix_companies_company_type "
               "ON companies (company_type)")
    op.execute("""
        CREATE TABLE market_state (
            symbol VARCHAR(20) NOT NULL,
            as_of DATE NOT NULL,
            asm_stage VARCHAR(16),
            gsm_stage VARCHAR(16),
            esm_stage VARCHAR(16),
            price_band VARCHAR(8),
            source_payload JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, as_of)
        )
    """)
    op.execute("CREATE INDEX ix_market_state_asof ON market_state (as_of)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS market_state")
    op.execute("ALTER TABLE companies DROP COLUMN IF EXISTS is_fund_scheme")
    op.execute("ALTER TABLE companies DROP COLUMN IF EXISTS type_detected_at")
    op.execute("ALTER TABLE companies DROP COLUMN IF EXISTS type_confidence")
    op.execute("ALTER TABLE companies DROP COLUMN IF EXISTS type_overlays")
    op.execute("ALTER TABLE companies DROP COLUMN IF EXISTS company_type")

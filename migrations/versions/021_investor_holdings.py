"""Investor (named >=1% shareholder) holdings — marquee-investor tracking.

One row per (company, quarter, named holder) extracted from the SHP XBRL's
>1% named-shareholder table. Powers the stock-centric "notable holders" view
and the investor-centric portfolio/QoQ-change panel.

Revision ID: 021
Revises: 020
Create Date: 2026-07-08 00:00:00.000000
"""
from alembic import op


revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE investor_holdings (
            id               serial PRIMARY KEY,
            symbol           varchar(20) NOT NULL REFERENCES companies(symbol) ON DELETE CASCADE,
            quarter_end      date NOT NULL,
            holder_name      text NOT NULL,
            holder_name_norm text NOT NULL,
            category         varchar(24),
            holding_pct      double precision,
            shares_held      bigint,
            source           varchar(20) NOT NULL,
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_investor_holding UNIQUE (symbol, quarter_end, holder_name)
        )
    """)
    op.execute("CREATE INDEX ix_investor_holdings_norm ON investor_holdings (holder_name_norm)")
    op.execute("CREATE INDEX ix_investor_holdings_symbol_q ON investor_holdings (symbol, quarter_end DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS investor_holdings")

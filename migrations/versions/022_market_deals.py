"""Bulk & block deals — intra-quarter smart-money flow (faster than SHP).

One row per disclosed bulk/block deal (NSE + BSE). Market-making / HFT churn is
flagged (is_excluded + exclude_reason) rather than deleted, so the filter is
auditable and reversible. Powers the "Recent Deals" flow in the Smart Money tab.

Revision ID: 022
Revises: 021
Create Date: 2026-07-13 00:00:00.000000
"""
from alembic import op


revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE market_deals (
            id               serial PRIMARY KEY,
            deal_date        date NOT NULL,
            exchange         varchar(4) NOT NULL,
            deal_type        varchar(8) NOT NULL,          -- bulk | block
            symbol           varchar(20),                  -- resolved NSE-style symbol (nullable: off-universe names)
            company_name     text,
            client_name      text NOT NULL,
            client_name_norm text NOT NULL,
            side             varchar(4) NOT NULL,          -- BUY | SELL
            quantity         bigint,
            price            double precision,
            value_cr         double precision,
            marquee_key      varchar(40),                  -- resolve_investor() key, if any
            is_excluded      boolean NOT NULL DEFAULT false,
            exclude_reason   varchar(24),                  -- market_maker | churn | sub_threshold
            created_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_market_deal UNIQUE (deal_date, exchange, deal_type, client_name, symbol, side, quantity, price)
        )
    """)
    op.execute("CREATE INDEX ix_market_deals_norm ON market_deals (client_name_norm)")
    op.execute("CREATE INDEX ix_market_deals_symbol_date ON market_deals (symbol, deal_date DESC)")
    op.execute("CREATE INDEX ix_market_deals_date ON market_deals (deal_date DESC) WHERE is_excluded = false")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS market_deals")

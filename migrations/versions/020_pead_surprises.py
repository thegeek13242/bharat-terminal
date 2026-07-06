"""PEAD earnings-surprise productization (study pead_v2_2026_07).

Three DDL changes:
1. financial_results.announced_at — the result's announcement timestamp
   (NSE: from nse_filings filing/broadcast date via CLI backfill; BSE:
   captured from the results index going forward). Needed because drift is
   measured from announcement, not period end.
2. signal_outcomes.impact_report_id becomes nullable — PEAD signals are
   deterministic (no LLM impact report behind them).
3. earnings_surprises — one row per (symbol, quarter): SUE components,
   trailing-distribution percentile/decile, liquidity screen, provenance.

Backfills are CLI steps (kb.manage pead-backfill), never migration DML.

Revision ID: 020
Revises: 019
Create Date: 2026-07-04 00:00:00.000000
"""
from alembic import op


revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE financial_results ADD COLUMN announced_at timestamptz"
    )
    op.execute(
        "CREATE INDEX ix_financial_results_announced "
        "ON financial_results (symbol, announced_at)"
    )
    op.execute(
        "ALTER TABLE signal_outcomes ALTER COLUMN impact_report_id DROP NOT NULL"
    )
    op.execute("""
        CREATE TABLE earnings_surprises (
            id                     serial PRIMARY KEY,
            symbol                 varchar(20) NOT NULL REFERENCES companies(symbol) ON DELETE CASCADE,
            period_end             date NOT NULL,
            announced_at           timestamptz,
            eps_basic              double precision,
            revenue_cr             double precision,
            sue_eps                double precision,
            sue_revenue            double precision,
            sue_composite          double precision,
            percentile             double precision,
            decile                 integer,
            liquidity_pass         boolean,
            median_traded_value_cr double precision,
            reference_n            integer,
            study_version          varchar(20) NOT NULL,
            signal_emitted         boolean NOT NULL DEFAULT false,
            created_at             timestamptz NOT NULL DEFAULT now(),
            updated_at             timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_earnings_surprise UNIQUE (symbol, period_end)
        )
    """)
    op.execute(
        "CREATE INDEX ix_earnings_surprises_announced "
        "ON earnings_surprises (announced_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_earnings_surprises_decile ON earnings_surprises (decile)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS earnings_surprises")
    op.execute("ALTER TABLE signal_outcomes ALTER COLUMN impact_report_id SET NOT NULL")
    op.execute("DROP INDEX IF EXISTS ix_financial_results_announced")
    op.execute("ALTER TABLE financial_results DROP COLUMN IF EXISTS announced_at")

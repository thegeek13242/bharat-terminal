"""Guidance ledger + outcomes (C2, MANAGEMENT_COMMENTARY_2026-07-31.md).

guidance_ledger: a normalized projection of transcript_guidance — one row per
extracted guidance statement, with CANONICAL join keys (metric_key, period_key,
unit_key) that the raw table lacks (1,351 distinct free-text metric labels over
2,589 rows as of 2026-07-31; unit-spelling noise was silently degrading
change-verdicts). Lifecycle: active → superseded (newer statement, same
symbol+metric_key+period_key) → resolved (outcome scored). The standing
guidance state of a company = its active rows.

guidance_outcomes: delivered-vs-guided scoring against financial_results
actuals, for the class-a metrics with exact formulas (revenue, revenue growth,
PAT, EBITDA margin, EPS, tax rate). This is the credibility scaffold: as of
2026-07-31 exactly ONE pair resolves (INDHOTEL) — the table exists to ACCRUE;
any credibility multiplier stays OFF until companies reach >= 2 resolved pairs
(earliest meaningful population: FY27 actuals, Apr-May 2027).

Revision ID: 034
Revises: 033
Create Date: 2026-07-31 00:00:00.000000
"""
from alembic import op


revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE guidance_ledger (
            id BIGSERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            metric_key TEXT NOT NULL,
            metric_raw TEXT NOT NULL,
            period_key TEXT,
            period_end DATE,
            value_low DOUBLE PRECISION,
            value_high DOUBLE PRECISION,
            unit_key TEXT,
            unit_raw TEXT,
            better_direction VARCHAR(6),
            source_guidance_id INT NOT NULL UNIQUE,
            transcript_id INT,
            stated_at TIMESTAMPTZ NOT NULL,
            change_verdict VARCHAR(12) NOT NULL DEFAULT 'new',
            restatements INT NOT NULL DEFAULT 0,
            status VARCHAR(10) NOT NULL DEFAULT 'active',
            scoreable BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_guidance_ledger_symbol "
               "ON guidance_ledger (symbol, status)")
    op.execute("CREATE INDEX ix_guidance_ledger_group "
               "ON guidance_ledger (symbol, metric_key, period_key)")
    op.execute("CREATE INDEX ix_guidance_ledger_resolution "
               "ON guidance_ledger (period_end) "
               "WHERE scoreable AND status = 'active'")
    op.execute("""
        CREATE TABLE guidance_outcomes (
            id BIGSERIAL PRIMARY KEY,
            ledger_id BIGINT NOT NULL UNIQUE,
            symbol VARCHAR(20) NOT NULL,
            metric_key TEXT NOT NULL,
            period_key TEXT NOT NULL,
            guided_low DOUBLE PRECISION,
            guided_high DOUBLE PRECISION,
            delivered DOUBLE PRECISION NOT NULL,
            delivered_verdict VARCHAR(6) NOT NULL,
            met BOOLEAN,
            basis_consolidated BOOLEAN,
            formula TEXT NOT NULL,
            actual_period_end DATE NOT NULL,
            actual_announced_at TIMESTAMPTZ,
            ambiguity_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
            scored_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_guidance_outcomes_symbol "
               "ON guidance_outcomes (symbol)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS guidance_outcomes")
    op.execute("DROP TABLE IF EXISTS guidance_ledger")

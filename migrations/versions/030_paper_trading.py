"""Paper-trading tables (internal automated-trading pilot, Phase C).

paper_trades: one row per simulated position with full decision provenance
(which filing, which model artifact, which gate values) — the audit-trail
discipline live trading will require, exercised from day one. The
(impact_id, symbol, strategy) unique key is the order-idempotency lesson:
a replayed Kafka message must never double-open a position.

paper_equity_snapshots: periodic portfolio marks for the equity curve,
max-drawdown and Sharpe on the dashboard.

Revision ID: 030
Revises: 029
Create Date: 2026-07-28 00:00:00.000000
"""
from alembic import op


revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE paper_trades (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            strategy VARCHAR(40) NOT NULL,
            impact_id VARCHAR(64),
            news_id VARCHAR(64),
            symbol VARCHAR(20) NOT NULL,
            side VARCHAR(5) NOT NULL CHECK (side IN ('LONG', 'SHORT')),
            instrument VARCHAR(4) NOT NULL CHECK (instrument IN ('CASH', 'FUT')),
            qty INTEGER NOT NULL,
            entry_price DOUBLE PRECISION,
            entry_at TIMESTAMPTZ,
            exit_price DOUBLE PRECISION,
            exit_at TIMESTAMPTZ,
            exit_reason VARCHAR(20),
            status VARCHAR(10) NOT NULL DEFAULT 'OPEN'
                CHECK (status IN ('OPEN', 'CLOSED', 'REJECTED')),
            mtm_price DOUBLE PRECISION,
            mtm_at TIMESTAMPTZ,
            pnl DOUBLE PRECISION,
            provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (impact_id, symbol, strategy)
        )
    """)
    op.execute("CREATE INDEX ix_paper_trades_status ON paper_trades (status)")
    op.execute("CREATE INDEX ix_paper_trades_created ON paper_trades (created_at)")
    op.execute("CREATE INDEX ix_paper_trades_symbol ON paper_trades (symbol)")
    op.execute("""
        CREATE TABLE paper_equity_snapshots (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            equity DOUBLE PRECISION NOT NULL,
            realized_pnl DOUBLE PRECISION NOT NULL,
            unrealized_pnl DOUBLE PRECISION NOT NULL,
            gross_exposure DOUBLE PRECISION NOT NULL,
            n_open INTEGER NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_paper_equity_ts ON paper_equity_snapshots (ts)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS paper_equity_snapshots")
    op.execute("DROP TABLE IF EXISTS paper_trades")

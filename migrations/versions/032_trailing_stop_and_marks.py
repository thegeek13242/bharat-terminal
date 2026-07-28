"""Vol-scaled trailing stops + intraday absorption marks.

trail_extreme / entry_vol60 on paper_trades: the trailing stop is
volatility-scaled (founder decision 2026-07-28: wrong classifications must
cause LIMITED loss) — trail distance = PAPER_TRAIL_SIGMA x sigma60,
anchored to the position's favorable extreme. pead_long is exempt (its
validated edge is an unconditional 20d hold).

paper_signal_marks: LTP snapshots at decision +5/15/30/60min for every
DURING-SESSION hop-0 filing impact (traded or not) — measures the
absorption curve per cell, i.e. how much of a reaction our ~90s latency
can actually reach. Daily OHLC cannot answer this; one results season of
this exhaust can.

Revision ID: 032
Revises: 031
Create Date: 2026-07-28 00:00:00.000000
"""
from alembic import op


revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE paper_trades ADD COLUMN trail_extreme DOUBLE PRECISION")
    op.execute("ALTER TABLE paper_trades ADD COLUMN entry_vol60 DOUBLE PRECISION")
    op.execute("""
        CREATE TABLE paper_signal_marks (
            id BIGSERIAL PRIMARY KEY,
            impact_id VARCHAR(64) NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            magnitude INTEGER,
            sentiment VARCHAR(10),
            mark VARCHAR(8) NOT NULL,      -- t0 | t5 | t15 | t30 | t60
            ltp DOUBLE PRECISION NOT NULL,
            ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (impact_id, symbol, mark)
        )
    """)
    op.execute("CREATE INDEX ix_signal_marks_ts ON paper_signal_marks (ts)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS paper_signal_marks")
    op.execute("ALTER TABLE paper_trades DROP COLUMN IF EXISTS entry_vol60")
    op.execute("ALTER TABLE paper_trades DROP COLUMN IF EXISTS trail_extreme")

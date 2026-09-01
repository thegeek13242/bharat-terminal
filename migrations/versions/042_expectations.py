"""E1 expectations — point-in-time, append-only, one row per estimator.

Stage 4 of the E-series. Deliberately NOT a single blended "composite":
Stage 4B measured that guidance is a worse fundamental forecaster than the
statistical GBM (−41% revenue, −26% ebitda on well-calibrated conversions),
and that a 50/50 blend scores WORSE than either input. But per Stage 4A,
E1's target is what the MARKET expected, not what the fundamentals will be,
and guidance is what the market anchors on. So both expectations are stored
SIDE BY SIDE, and E2 computes two surprises from them:

    fundamental surprise   = actual vs the `gbm` estimator
    expectation-gap surprise = actual vs the `guidance` estimator

Stage 5 then tests which predicts announcement reactions. Blending them now
would destroy exactly the comparison the go/no-go depends on.

APPEND-ONLY BY CONSTRUCTION. Rows are never updated; a new model_version
writes new rows. `as_of` is the information cutoff the row was computed
under and `inputs_max_ts` is the latest timestamp of any input that fed it
— together they make leakage mechanically auditable
(bharat_terminal/research/leakage_audit.py asserts inputs_max_ts < the
event's announced_at for every row).

Revision ID: 042
Revises: 041
Create Date: 2026-09-01 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "expectations",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("metric", sa.String(length=20), nullable=False),
        sa.Column("estimator", sa.String(length=24), nullable=False),
        sa.Column("expected_value", sa.Float, nullable=False),
        sa.Column("band_low", sa.Float),
        sa.Column("band_high", sa.Float),
        sa.Column("tier", sa.String(length=4), nullable=False),
        sa.Column("basis", sa.String(length=64)),
        # Every input that fed this row, plus the latest input timestamp —
        # the leakage audit reads inputs_max_ts, so it must always be set.
        sa.Column("inputs_max_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("model_version", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("symbol", "period_end", "metric", "estimator",
                            "model_version", name="uq_expectation"),
    )
    op.create_index("ix_expectations_symbol_period", "expectations",
                    ["symbol", "period_end"])
    op.create_index("ix_expectations_estimator", "expectations",
                    ["estimator", "metric"])


def downgrade() -> None:
    op.drop_index("ix_expectations_estimator", table_name="expectations")
    op.drop_index("ix_expectations_symbol_period", table_name="expectations")
    op.drop_table("expectations")

"""Fix derivative_ticks/derivatives_eod to allow NULL strike for futures

Migration 008 used (timestamp, symbol, instrument_type, expiry, strike) as
the PRIMARY KEY for both derivative_ticks and derivatives_eod. PostgreSQL
implies NOT NULL on all PK columns, but FUT rows have strike = NULL by
definition. Result: every futures insert rejected with
  null value in column "strike" of relation "derivative_ticks" violates
  not-null constraint

This migration:
  • Drops the composite PK on both tables
  • Adds an auto-incrementing `id` BIGSERIAL PRIMARY KEY
  • Recreates the natural-key uniqueness as a UNIQUE INDEX that uses
    COALESCE(strike, -1) so NULL strike rows compare equal to each other
    (preserving idempotent UPSERT-on-conflict semantics for futures and
    options alike).
  • ALTER COLUMN strike DROP NOT NULL.

Revision ID: 009
Revises: 008
Create Date: 2026-05-14 14:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── derivative_ticks ─────────────────────────────────────────────
    op.execute("ALTER TABLE derivative_ticks DROP CONSTRAINT pk_derivative_ticks")
    op.execute("ALTER TABLE derivative_ticks ALTER COLUMN strike DROP NOT NULL")
    op.add_column(
        "derivative_ticks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
    )
    op.create_primary_key("pk_derivative_ticks", "derivative_ticks", ["id"])
    # Natural-key uniqueness with COALESCE so NULL strike rows compare equal
    op.execute("""
        CREATE UNIQUE INDEX uq_dt_natural
        ON derivative_ticks (
            timestamp_utc, symbol, instrument_type, expiry,
            COALESCE(strike, -1)
        )
    """)

    # ── derivatives_eod ──────────────────────────────────────────────
    op.execute("ALTER TABLE derivatives_eod DROP CONSTRAINT pk_derivatives_eod")
    op.execute("ALTER TABLE derivatives_eod ALTER COLUMN strike DROP NOT NULL")
    op.add_column(
        "derivatives_eod",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
    )
    op.create_primary_key("pk_derivatives_eod", "derivatives_eod", ["id"])
    op.execute("""
        CREATE UNIQUE INDEX uq_de_natural
        ON derivatives_eod (
            date, symbol, instrument_type, expiry,
            COALESCE(strike, -1)
        )
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_de_natural")
    op.execute("ALTER TABLE derivatives_eod DROP CONSTRAINT pk_derivatives_eod")
    op.drop_column("derivatives_eod", "id")
    op.execute("ALTER TABLE derivatives_eod ALTER COLUMN strike SET NOT NULL")
    op.create_primary_key(
        "pk_derivatives_eod", "derivatives_eod",
        ["date", "symbol", "instrument_type", "expiry", "strike"],
    )

    op.execute("DROP INDEX IF EXISTS uq_dt_natural")
    op.execute("ALTER TABLE derivative_ticks DROP CONSTRAINT pk_derivative_ticks")
    op.drop_column("derivative_ticks", "id")
    op.execute("ALTER TABLE derivative_ticks ALTER COLUMN strike SET NOT NULL")
    op.create_primary_key(
        "pk_derivative_ticks", "derivative_ticks",
        ["timestamp_utc", "symbol", "instrument_type", "expiry", "strike"],
    )

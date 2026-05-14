"""Derivatives schema — F&O ticks, EOD snapshots, participant-wise OI

Three tables for the F&O data layer ingested via Angel One SmartAPI WebSocket
(stock + index futures, intraday OI) and via NSE CSV daily publications
(participant-wise positioning by client type).

  • derivative_ticks   — selectively-persisted intraday OI events. Raw
                         WebSocket ticks flow through Kafka `raw.fno.in`;
                         the fno-engine consumer filters to OI-significant
                         events (Δ ≥ 1%) plus 60s heartbeats to keep
                         storage bounded.

  • derivatives_eod    — end-of-day snapshot of every traded contract.
                         Populated by a daily NSE option-chain pull.

  • participant_oi     — daily FII / DII / Pro / Client positioning by
                         instrument type. Pulled from NSE archives CSV.
                         The leading-indicator signal for index direction.

All three tables index on (date, symbol) for fast time-range queries
during news propagation. Composite primary keys avoid auto-generated
IDs since rows are naturally unique by (date, symbol, instrument, expiry,
strike) tuples.

Revision ID: 008
Revises: 007
Create Date: 2026-05-11 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── derivative_ticks ──────────────────────────────────────────────
    # Selectively-persisted intraday OI events. Raw tick rate is too high
    # to store (5M+ ticks/day across 60 contracts) — fno-engine filters
    # to OI-Δ ≥ threshold plus a 60s heartbeat per contract.
    op.create_table(
        "derivative_ticks",
        sa.Column("timestamp_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False,
                  comment="Underlying symbol: RELIANCE, NIFTY, BANKNIFTY, …"),
        sa.Column("instrument_type", sa.String(10), nullable=False,
                  comment="FUT | CE | PE"),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", sa.Numeric(10, 2), nullable=True,
                  comment="NULL for futures; strike price for options"),
        sa.Column("token", sa.String(20), nullable=False,
                  comment="Angel One scrip token"),
        sa.Column("ltp", sa.Numeric(12, 2), nullable=True),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("change_in_oi", sa.BigInteger(), nullable=True),
        sa.Column("volume_day", sa.BigInteger(), nullable=True),
        sa.Column("iv", sa.Numeric(6, 2), nullable=True,
                  comment="Implied volatility for options; NULL for futures"),
        sa.Column("total_buy_qty", sa.BigInteger(), nullable=True),
        sa.Column("total_sell_qty", sa.BigInteger(), nullable=True),
        sa.Column("trigger_reason", sa.String(20), nullable=True,
                  comment="oi_delta | heartbeat | volume_spike | session_close"),
        sa.PrimaryKeyConstraint(
            "timestamp_utc", "symbol", "instrument_type", "expiry", "strike",
            name="pk_derivative_ticks",
        ),
        sa.CheckConstraint(
            "instrument_type IN ('FUT','CE','PE')",
            name="ck_dt_instrument_type",
        ),
    )
    # The hottest query: "show me OI trend for RELIANCE futures over last 5 days"
    op.create_index(
        "ix_dt_symbol_ts",
        "derivative_ticks",
        ["symbol", sa.text("timestamp_utc DESC")],
    )
    # For sweeping all contracts of one expiry (max-pain, PCR calcs)
    op.create_index(
        "ix_dt_expiry_symbol",
        "derivative_ticks",
        ["expiry", "symbol", "instrument_type"],
    )

    # ── derivatives_eod ───────────────────────────────────────────────
    # End-of-day snapshot — one row per traded contract per day. The full
    # option chain for NIFTY/BANKNIFTY plus stock futures gets persisted
    # here after market close via the daily NSE batch.
    op.create_table(
        "derivatives_eod",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("instrument_type", sa.String(10), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", sa.Numeric(10, 2), nullable=True),
        sa.Column("close", sa.Numeric(12, 2), nullable=True),
        sa.Column("settle_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("change_in_oi", sa.BigInteger(), nullable=True),
        sa.Column("volume_day", sa.BigInteger(), nullable=True),
        sa.Column("iv", sa.Numeric(6, 2), nullable=True),
        sa.Column("delta", sa.Numeric(6, 4), nullable=True),
        sa.Column("gamma", sa.Numeric(8, 6), nullable=True),
        sa.Column("theta", sa.Numeric(10, 2), nullable=True),
        sa.Column("vega", sa.Numeric(10, 2), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="nse_option_chain"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint(
            "date", "symbol", "instrument_type", "expiry", "strike",
            name="pk_derivatives_eod",
        ),
        sa.CheckConstraint(
            "instrument_type IN ('FUT','CE','PE')",
            name="ck_de_instrument_type",
        ),
    )
    op.create_index(
        "ix_de_symbol_date",
        "derivatives_eod",
        ["symbol", sa.text("date DESC")],
    )

    # ── participant_oi ────────────────────────────────────────────────
    # Daily NSE FII/DII/Pro/Client positioning. The single most valuable
    # F&O dataset for direction-of-flow signals.
    op.create_table(
        "participant_oi",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("client_type", sa.String(10), nullable=False,
                  comment="CLIENT | DII | FII | PRO"),
        sa.Column("instrument_type", sa.String(20), nullable=False,
                  comment="FUTIDX | FUTSTK | OPTIDX_CE | OPTIDX_PE | OPTSTK_CE | OPTSTK_PE"),
        sa.Column("long_contracts", sa.BigInteger(), nullable=True),
        sa.Column("short_contracts", sa.BigInteger(), nullable=True),
        # net = long - short, generated column so we don't drift
        sa.Column(
            "net_contracts",
            sa.BigInteger(),
            sa.Computed("COALESCE(long_contracts, 0) - COALESCE(short_contracts, 0)",
                        persisted=True),
        ),
        sa.Column("source_csv", sa.String(120), nullable=True,
                  comment="Origin filename for audit"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint(
            "date", "client_type", "instrument_type",
            name="pk_participant_oi",
        ),
        sa.CheckConstraint(
            "client_type IN ('CLIENT','DII','FII','PRO')",
            name="ck_poi_client_type",
        ),
    )
    op.create_index(
        "ix_poi_client_date",
        "participant_oi",
        ["client_type", sa.text("date DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_poi_client_date", table_name="participant_oi")
    op.drop_table("participant_oi")
    op.drop_index("ix_de_symbol_date", table_name="derivatives_eod")
    op.drop_table("derivatives_eod")
    op.drop_index("ix_dt_expiry_symbol", table_name="derivative_ticks")
    op.drop_index("ix_dt_symbol_ts", table_name="derivative_ticks")
    op.drop_table("derivative_ticks")

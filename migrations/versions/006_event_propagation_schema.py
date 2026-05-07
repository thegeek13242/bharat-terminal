"""Event-propagation graph: macro events × 3-degree sectors × leaders × calibrated betas

This migration replaces the static `commodity/exposure_map.py` with a queryable
graph in PostgreSQL:

  • macro_events           : the 8 events from the trading-strategy framework
                              (crude_up, fed_hike, rbi_hike, vix_spike, …) plus
                              continuous-trigger events (commodity shocks).
  • event_degrees          : labels and transmission-channel narrative for each
                              of the 3 propagation degrees.
  • event_sector_edges     : event → sector at a given degree, with mechanism,
                              lag, prior-magnitude. This is the structural /
                              causal layer (hand-curated, not data-derived).
  • event_transmissions    : edges connecting degree N → degree N+1, gated on
                              live transmission signals (INR, FII, VIX, …).
  • triggers               : catalog of trigger variables (CRUDEOIL, BRENT, DXY,
                              USDINR, US10Y, INDIA_VIX, FED_FUNDS, …). One row
                              per quantitative variable we can regress against.
  • trigger_betas          : per-(symbol, trigger) calibrated beta with
                              provenance — so the same row can come from agent
                              research, regression calibration, or a manual
                              override and we know which is which.
  • fundamental_exposures  : citation-backed cost / revenue exposures parsed
                              from filings. Anchors the beta in real fundamentals.
  • sector_leaders         : ranked list of leaders per (event, degree, sector),
                              with a derivation_method so we can swap manual
                              ranking for empirical-beta ranking later.

Strict-mode contract: the signal generator queries `trigger_betas` to pick
companies for the impact report. If a (symbol, trigger) row is missing, the
symbol is dropped from the graph for that event. No fallback to defaults.

Revision ID: 006
Revises: 005
Create Date: 2026-05-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── triggers ──────────────────────────────────────────────────────────
    # Catalog of every quantitative input variable we regress / agent-research
    # equity exposure against. Distinguishes continuous (commodity, FX, rates)
    # from discrete (FOMC decision, RBI MPC, CPI print).
    op.create_table(
        "triggers",
        sa.Column("id", sa.String(40), primary_key=True),                  # e.g. "CRUDEOIL"
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),                  # continuous | discrete
        sa.Column("unit", sa.String(40), nullable=True),                   # "INR/bbl", "%", …
        sa.Column("data_source", sa.String(80), nullable=True),            # "MCX", "Yahoo:^TNX", …
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── macro_events ──────────────────────────────────────────────────────
    op.create_table(
        "macro_events",
        sa.Column("id", sa.String(40), primary_key=True),                  # "crude_up", "fed_hike", …
        sa.Column("label", sa.String(160), nullable=False),
        sa.Column("trigger_id", sa.String(40), sa.ForeignKey("triggers.id"), nullable=False),
        sa.Column("trigger_spec", sa.Text(), nullable=False),              # ">5% session move", "≥25bps surprise"
        sa.Column("propagation_chain", postgresql.JSONB, nullable=True),   # ["CAD widens", "INR depreciates", …]
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── event_degrees ─────────────────────────────────────────────────────
    op.create_table(
        "event_degrees",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(40), sa.ForeignKey("macro_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("degree", sa.Integer(), nullable=False),                 # 1, 2, 3
        sa.Column("label", sa.String(120), nullable=False),                # "Direct cost exposure"
        sa.Column("transmission_channel", sa.Text(), nullable=True),
        sa.UniqueConstraint("event_id", "degree", name="uq_event_degrees"),
    )

    # ── event_sector_edges ────────────────────────────────────────────────
    # Event × degree × sector edge with mechanism + lag + prior magnitude.
    # The structural / causal layer — hand-curated.
    op.create_table(
        "event_sector_edges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(40), sa.ForeignKey("macro_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("degree", sa.Integer(), nullable=False),                 # 1 | 2 | 3
        sa.Column("sector", sa.String(120), nullable=False),               # "Aviation", "Paints", …
        sa.Column("direction", sa.String(10), nullable=False),             # bull | bear | mixed | neutral
        sa.Column("magnitude_prior", sa.Integer(), nullable=False),        # 1-5 prior, refined by data
        sa.Column("lag_text", sa.String(40), nullable=True),               # "Intraday", "T+7-21"
        sa.Column("lag_min_days", sa.Integer(), nullable=True),
        sa.Column("lag_max_days", sa.Integer(), nullable=True),
        sa.Column("mechanism", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("event_id", "degree", "sector", name="uq_event_sector_edge"),
    )
    op.create_index("ix_event_sector_edges_event", "event_sector_edges", ["event_id"])
    op.create_index("ix_event_sector_edges_sector", "event_sector_edges", ["sector"])

    # ── event_transmissions ───────────────────────────────────────────────
    # Edges that connect degree N → degree N+1, gated on live signals.
    # Frontend uses these to decide whether to "light up" the next degree.
    op.create_table(
        "event_transmissions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(40), sa.ForeignKey("macro_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_degree", sa.Integer(), nullable=False),
        sa.Column("to_degree", sa.Integer(), nullable=False),
        sa.Column("gating_trigger_id", sa.String(40), sa.ForeignKey("triggers.id"), nullable=False),
        sa.Column("confirms_threshold", sa.Float(), nullable=True),         # signed pct/std move that confirms
        sa.Column("refutes_threshold", sa.Float(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "event_id", "from_degree", "to_degree", "gating_trigger_id",
            name="uq_event_transmission"
        ),
    )

    # ── trigger_betas ─────────────────────────────────────────────────────
    # Calibrated beta: signed sensitivity of a stock's return to a 1-unit
    # move in a trigger. Source-agnostic — the same row can come from agent
    # research, regression calibration, or manual entry. The signal layer
    # picks the highest-priority source per (symbol, trigger).
    op.create_table(
        "trigger_betas",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(20), sa.ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger_id", sa.String(40), sa.ForeignKey("triggers.id"), nullable=False),
        sa.Column("beta", sa.Float(), nullable=False),                      # signed
        sa.Column("beta_low", sa.Float(), nullable=True),                   # 80% / 90% CI lower
        sa.Column("beta_high", sa.Float(), nullable=True),
        sa.Column("std_err", sa.Float(), nullable=True),
        sa.Column("t_stat", sa.Float(), nullable=True),
        sa.Column("r_squared", sa.Float(), nullable=True),
        sa.Column("n_observations", sa.Integer(), nullable=True),
        sa.Column("sample_start", sa.Date(), nullable=True),
        sa.Column("sample_end", sa.Date(), nullable=True),
        sa.Column("derivation_method", sa.String(40), nullable=False),      # agent_research | regression | manual
        sa.Column("model_used", sa.String(120), nullable=True),             # "claude-sonnet-4-6", "ols-12mo-rolling"
        sa.Column("confidence", sa.String(10), nullable=False, server_default="low"),  # low | medium | high
        sa.Column("rationale", sa.Text(), nullable=True),                   # short prose summary from agent
        sa.Column("citations", postgresql.JSONB, nullable=True),            # [{url, source, quote, period}]
        sa.Column("calibrated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True), # next-quarter refresh hint
        sa.UniqueConstraint("symbol", "trigger_id", "derivation_method", name="uq_trigger_beta_method"),
    )
    op.create_index("ix_trigger_betas_symbol_trigger", "trigger_betas", ["symbol", "trigger_id"])
    op.create_index("ix_trigger_betas_trigger", "trigger_betas", ["trigger_id"])

    # ── fundamental_exposures ─────────────────────────────────────────────
    # Citation-backed exposures parsed from BSE/NSE XBRL filings + analyst
    # commentary. Anchors a beta — without a fundamental basis we don't trust
    # the regression (correlation-without-causation guard).
    op.create_table(
        "fundamental_exposures",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(20), sa.ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger_id", sa.String(40), sa.ForeignKey("triggers.id"), nullable=False),
        sa.Column("exposure_kind", sa.String(20), nullable=False),          # cost | revenue | mixed
        sa.Column("pct_of_total", sa.Float(), nullable=True),               # e.g. 0.34 → 34% of opex
        sa.Column("total_basis", sa.String(40), nullable=True),             # "FY25 opex", "FY25 revenue"
        sa.Column("mechanism", sa.Text(), nullable=False),
        sa.Column("source_doc_url", sa.Text(), nullable=True),
        sa.Column("source_period", sa.String(40), nullable=True),           # "Q4FY25", "FY25 Annual Report"
        sa.Column("source_kind", sa.String(40), nullable=True),             # "bse_xbrl", "ar_pdf", "earnings_call"
        sa.Column("citations", postgresql.JSONB, nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("symbol", "trigger_id", "source_period", name="uq_fundamental_exposure"),
    )
    op.create_index("ix_fundamental_exposures_symbol", "fundamental_exposures", ["symbol"])
    op.create_index("ix_fundamental_exposures_trigger", "fundamental_exposures", ["trigger_id"])

    # ── sector_leaders ────────────────────────────────────────────────────
    # Ranked leaders per (event, degree, sector). Initially can be seeded
    # from manual lists, then re-ranked by trigger_betas.beta magnitude as
    # calibration matures.
    op.create_table(
        "sector_leaders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(40), sa.ForeignKey("macro_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("degree", sa.Integer(), nullable=False),
        sa.Column("sector", sa.String(120), nullable=False),
        sa.Column("symbol", sa.String(20), sa.ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),                    # 1 = top leader
        sa.Column("derivation_method", sa.String(40), nullable=False),      # manual | beta_rank | mcap_rank
        sa.Column("last_calibrated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("event_id", "degree", "sector", "symbol", name="uq_sector_leader"),
    )
    op.create_index("ix_sector_leaders_event", "sector_leaders", ["event_id", "degree"])

    # ── universe_membership ───────────────────────────────────────────────
    # NIFTY 50, BANKNIFTY, NIFTY MIDCAP — keeps the agent runner honest about
    # which symbols are in scope.
    op.create_table(
        "universe_membership",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("universe_name", sa.String(40), nullable=False),         # "NIFTY50", "BANKNIFTY"
        sa.Column("symbol", sa.String(20), sa.ForeignKey("companies.symbol", ondelete="CASCADE"), nullable=False),
        sa.Column("weight_pct", sa.Float(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("universe_name", "symbol", name="uq_universe_member"),
    )
    op.create_index("ix_universe_universe", "universe_membership", ["universe_name"])


def downgrade() -> None:
    op.drop_table("universe_membership")
    op.drop_index("ix_sector_leaders_event", table_name="sector_leaders")
    op.drop_table("sector_leaders")
    op.drop_index("ix_fundamental_exposures_trigger", table_name="fundamental_exposures")
    op.drop_index("ix_fundamental_exposures_symbol", table_name="fundamental_exposures")
    op.drop_table("fundamental_exposures")
    op.drop_index("ix_trigger_betas_trigger", table_name="trigger_betas")
    op.drop_index("ix_trigger_betas_symbol_trigger", table_name="trigger_betas")
    op.drop_table("trigger_betas")
    op.drop_table("event_transmissions")
    op.drop_index("ix_event_sector_edges_sector", table_name="event_sector_edges")
    op.drop_index("ix_event_sector_edges_event", table_name="event_sector_edges")
    op.drop_table("event_sector_edges")
    op.drop_table("event_degrees")
    op.drop_table("macro_events")
    op.drop_table("triggers")

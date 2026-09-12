"""Research factory ledger.

Creates the `factory` schema: the durable record of every hypothesis,
experiment, run, statistical test, gate decision, strategy, divergence,
failure, agent run and daily report.

Design rules encoded here (RESEARCH_FACTORY_SPEC_v2 §22):
  * Append-only by default. `hypothesis_status` is the single mutable table.
  * `experiments.spec_hash` is UNIQUE — duplicate research is a constraint
    violation, not a budget line.
  * `experiment_runs` is unique on (experiment, sweep point, dataset version,
    code commit), so the same work cannot be silently recorded twice and a
    re-run on the same inputs must reproduce `result_hash`.
  * Three NOLOGIN group roles separate "can write research" from "can decide
    promotions". Login users are provisioned separately with passwords from
    the environment — never in a migration.

NOT APPLIED automatically. Applying this needs founder approval.

Revision ID: 043
Revises: 042
Create Date: 2026-09-12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None

SCHEMA = "factory"

# Ladder states. Tier H goes through OUT_OF_SAMPLE; Tier F (features with only
# months of history) goes through SHADOW instead — forward evidence is the only
# out-of-sample that exists for them.
STATUSES = (
    "IDEA", "SIMULATED", "ROBUST", "OUT_OF_SAMPLE", "SHADOW", "SHADOW_VERIFIED",
    "PAPER", "PAPER_VERIFIED", "PRODUCTION_CANDIDATE",
    "REJECTED", "INCONCLUSIVE", "QUARANTINED", "RETIRED",
)


def _jsonb(name, nullable=True, default_empty=False):
    kw = {}
    if default_empty:
        kw["server_default"] = sa.text("'{}'::jsonb")
    return sa.Column(name, postgresql.JSONB, nullable=nullable, **kw)


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.create_table(
        "research_families",
        sa.Column("family_id", sa.String(64), primary_key=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("horizon", sa.String(16), nullable=False),
        sa.Column("data_tier", sa.String(1), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("budget_cap_usd", sa.Numeric(10, 2), nullable=False, server_default="50"),
        sa.Column("killed_reason", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )

    op.create_table(
        "research_questions",
        sa.Column("question_id", sa.String(16), primary_key=True),
        sa.Column("family_id", sa.String(64), sa.ForeignKey(f"{SCHEMA}.research_families.family_id")),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        _jsonb("evidence_refs", default_empty=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        schema=SCHEMA,
    )

    op.create_table(
        "hypotheses",
        sa.Column("hypothesis_id", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("family_id", sa.String(64), nullable=False),
        sa.Column("question_id", sa.String(16)),
        _jsonb("claim", nullable=False),
        sa.Column("spec_yaml", sa.Text, nullable=False),
        _jsonb("spec_json", nullable=False),
        sa.Column("horizon", sa.String(16), nullable=False),
        sa.Column("data_tier", sa.String(1), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("hypothesis_id", "version"),
        schema=SCHEMA,
    )

    # The only mutable table in the schema, and only the gate role may write it.
    op.create_table(
        "hypothesis_status",
        sa.Column("hypothesis_id", sa.String(16), primary_key=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("last_decision_id", sa.BigInteger),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")",
            name="ck_hypothesis_status",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "hypothesis_lineage",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("child_id", sa.String(16), nullable=False),
        sa.Column("child_version", sa.Integer, nullable=False),
        sa.Column("parent_id", sa.String(16), nullable=False),
        sa.Column("parent_version", sa.Integer, nullable=False),
        sa.Column("op", sa.String(20), nullable=False),
        _jsonb("diff", default_empty=True),
        sa.UniqueConstraint("child_id", "child_version", "parent_id", "parent_version",
                            name="uq_lineage_edge"),
        schema=SCHEMA,
    )

    op.create_table(
        "feature_versions",
        sa.Column("feature_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("source_table", sa.String(64), nullable=False),
        sa.Column("data_tier", sa.String(1), nullable=False),
        sa.Column("pit_clocks", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("history_start", sa.Date, nullable=False),
        sa.Column("coverage", sa.Float),           # NULL = unmeasured, and that blocks controls
        sa.Column("definition_ref", sa.Text, nullable=False),
        sa.Column("code_commit", sa.String(40)),
        _jsonb("integrity_flags", default_empty=True),
        sa.Column("deprecated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("feature_id", "version"),
        schema=SCHEMA,
    )

    op.create_table(
        "dataset_versions",
        sa.Column("dataset_version", sa.String(32), primary_key=True),
        _jsonb("manifest", nullable=False),        # table -> {rows, max_date, sha256}
        _jsonb("integrity", nullable=False),       # gate results
        sa.Column("snapshot_path", sa.Text, nullable=False),
        sa.Column("sealed_from", sa.Date, nullable=False),
        _jsonb("embargoes", default_empty=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )

    op.create_table(
        "experiments",
        sa.Column("experiment_id", sa.String(16), primary_key=True),
        sa.Column("hypothesis_id", sa.String(16), nullable=False),
        sa.Column("hypothesis_version", sa.Integer, nullable=False),
        _jsonb("spec", nullable=False),
        sa.Column("spec_hash", sa.String(64), nullable=False, unique=True),
        _jsonb("sweep", default_empty=True),
        sa.Column("trial_count", sa.Integer, nullable=False, server_default="1"),
        _jsonb("bias_flags", default_empty=True),
        _jsonb("constraints", default_empty=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )

    op.create_table(
        "experiment_runs",
        sa.Column("run_id", sa.BigInteger, primary_key=True),
        sa.Column("experiment_id", sa.String(16),
                  sa.ForeignKey(f"{SCHEMA}.experiments.experiment_id"), nullable=False),
        _jsonb("sweep_point", default_empty=True),
        sa.Column("sweep_point_hash", sa.String(32), nullable=False, server_default=""),
        sa.Column("dataset_version", sa.String(32), nullable=False),
        _jsonb("feature_versions", nullable=False),
        sa.Column("code_commit", sa.String(40), nullable=False),
        sa.Column("seed", sa.Integer, nullable=False, server_default="42"),
        sa.Column("window_role", sa.String(16), nullable=False, server_default="in_sample"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("cpu_seconds", sa.Float),
        _jsonb("result"),
        sa.Column("result_hash", sa.String(64)),
        sa.Column("error", sa.Text),
        sa.Column("workflow_id", sa.String(128)),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("experiment_id", "sweep_point_hash", "dataset_version",
                            "code_commit", "window_role", name="uq_run_inputs"),
        schema=SCHEMA,
    )
    op.create_index("ix_factory_runs_experiment", "experiment_runs", ["experiment_id"], schema=SCHEMA)

    op.create_table(
        "stat_tests",
        sa.Column("test_id", sa.BigInteger, primary_key=True),
        sa.Column("run_id", sa.BigInteger, sa.ForeignKey(f"{SCHEMA}.experiment_runs.run_id")),
        sa.Column("kind", sa.String(24), nullable=False),
        _jsonb("inputs", default_empty=True),
        _jsonb("output", nullable=False),
        sa.Column("p_value", sa.Float),
        sa.Column("passed", sa.Boolean),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )

    op.create_table(
        "robustness_tests",
        sa.Column("test_id", sa.BigInteger, primary_key=True),
        sa.Column("experiment_id", sa.String(16), nullable=False),
        sa.Column("battery_test", sa.String(40), nullable=False),
        sa.Column("critical", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("requested_by", sa.String(16), nullable=False, server_default="battery"),
        sa.Column("run_id", sa.BigInteger),
        sa.Column("verdict", sa.String(16), nullable=False),
        _jsonb("details", default_empty=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )

    op.create_table(
        "gate_decisions",
        sa.Column("decision_id", sa.BigInteger, primary_key=True),
        sa.Column("hypothesis_id", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("from_status", sa.String(24), nullable=False),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("gate_version", sa.String(16), nullable=False),
        _jsonb("inputs", nullable=False),
        _jsonb("rule_results", nullable=False),
        _jsonb("llm_recommendation"),
        sa.Column("decided_by", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("decided_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )

    op.create_table(
        "holdout_access_log",
        sa.Column("access_id", sa.BigInteger, primary_key=True),
        sa.Column("hypothesis_id", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("run_id", sa.BigInteger),
        _jsonb("window", nullable=False),
        sa.Column("decision_id", sa.BigInteger),
        sa.Column("accessed_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("hypothesis_id", "version", name="uq_holdout_once_per_version"),
        schema=SCHEMA,
    )

    op.create_table(
        "alpha_wealth_ledger",
        sa.Column("entry_id", sa.BigInteger, primary_key=True),
        sa.Column("hypothesis_id", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("p_value", sa.Float, nullable=False),
        sa.Column("alpha_tested", sa.Float, nullable=False),
        sa.Column("rejected", sa.Boolean, nullable=False),
        sa.Column("wealth_before", sa.Float, nullable=False),
        sa.Column("wealth_after", sa.Float, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )

    op.create_table(
        "strategy_versions",
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("hypothesis_id", sa.String(16), nullable=False),
        sa.Column("hypothesis_version", sa.Integer, nullable=False),
        _jsonb("spec", nullable=False),
        _jsonb("params", default_empty=True),
        _jsonb("caps", nullable=False),
        _jsonb("expectation_envelope", nullable=False),
        sa.Column("mode", sa.String(8), nullable=False, server_default="shadow"),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("approved_by", sa.String(64)),
        sa.Column("code_commit", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("strategy_id", "version"),
        schema=SCHEMA,
    )

    op.create_table(
        "paper_links",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        # Matches public.paper_trades.strategy — the join back to live fills.
        sa.Column("paper_strategy_name", sa.String(40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("paper_strategy_name", name="uq_paper_strategy_name"),
        schema=SCHEMA,
    )

    op.create_table(
        "divergence_reports",
        sa.Column("report_id", sa.BigInteger, primary_key=True),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        _jsonb("window", nullable=False),
        _jsonb("metrics", nullable=False),
        sa.Column("classification", sa.String(24), nullable=False),
        _jsonb("evidence", default_empty=True),
        sa.Column("action", sa.String(16), nullable=False, server_default="none"),
        sa.Column("followup_question_id", sa.String(16)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )

    op.create_table(
        "failures",
        sa.Column("failure_id", sa.String(16), primary_key=True),
        sa.Column("hypothesis_id", sa.String(16)),
        sa.Column("version", sa.Integer),
        sa.Column("experiment_id", sa.String(16)),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("lesson", sa.Text, nullable=False),
        sa.Column("recommendation", sa.Text),
        _jsonb("evidence_refs", default_empty=True),
        # 'factory' | 'imported_unreverified' | 'human'. Imported lessons are
        # readable by agents but can never count as evidence at a gate.
        sa.Column("provenance", sa.String(24), nullable=False, server_default="factory"),
        sa.Column("quarantined", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("embedding_model", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )
    # pgvector is installed (0.8.6). Same 1024-dim space as memory_items.
    op.execute(f"ALTER TABLE {SCHEMA}.failures ADD COLUMN embedding vector(1024)")

    op.create_table(
        "frontier_snapshots",
        sa.Column("snapshot_id", sa.BigInteger, primary_key=True),
        _jsonb("ranking", nullable=False),
        _jsonb("allocation", nullable=False),
        sa.Column("memo_agent_run_id", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )

    op.create_table(
        "agent_versions",
        sa.Column("agent_version_id", sa.String(64), primary_key=True),
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("tools_sha256", sa.String(64), nullable=False),
        sa.Column("framework_version", sa.String(32)),
        sa.Column("runtime_arn", sa.Text),
        sa.Column("endpoint", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False, server_default="challenger"),
        _jsonb("e1_results", default_empty=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )

    op.create_table(
        "agent_runs",
        sa.Column("agent_run_id", sa.BigInteger, primary_key=True),
        sa.Column("agent_version_id", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(128)),
        _jsonb("task", nullable=False),
        _jsonb("result"),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("cache_read_tokens", sa.Integer),
        sa.Column("cache_write_tokens", sa.Integer),
        sa.Column("cost_usd", sa.Numeric(10, 4)),
        sa.Column("turns", sa.Integer),
        sa.Column("latency_ms", sa.Float),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("agentcore_session_id", sa.String(128)),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )
    op.create_index("ix_factory_agent_runs_version", "agent_runs",
                    ["agent_version_id", "created_at"], schema=SCHEMA)

    op.create_table(
        "eval_results",
        sa.Column("eval_id", sa.BigInteger, primary_key=True),
        sa.Column("layer", sa.String(4), nullable=False),          # E0..E7
        sa.Column("agent_version_id", sa.String(64)),
        sa.Column("agent_run_id", sa.BigInteger),
        sa.Column("evaluator", sa.String(64), nullable=False),
        sa.Column("score", sa.Float),
        sa.Column("label", sa.String(32)),
        _jsonb("details", default_empty=True),
        sa.Column("dataset_ref", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )
    op.create_index("ix_factory_eval_layer", "eval_results",
                    ["layer", "created_at"], schema=SCHEMA)

    op.create_table(
        "budget_ledger",
        sa.Column("entry_id", sa.BigInteger, primary_key=True),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("family_id", sa.String(64)),
        sa.Column("amount_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),        # measured | estimated
        _jsonb("ref", default_empty=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )
    op.create_index("ix_factory_budget_date", "budget_ledger", ["date"], schema=SCHEMA)

    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        _jsonb("payload", nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by", sa.String(64)),
        sa.Column("decision", sa.String(16)),
        schema=SCHEMA,
    )

    op.create_table(
        "daily_research_reports",
        sa.Column("report_date", sa.Date, primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        _jsonb("facts", nullable=False),
        sa.Column("html", sa.Text),
        sa.Column("text", sa.Text),
        _jsonb("claim_check", default_empty=True),
        _jsonb("delivery", default_empty=True),
        sa.Column("metric_pushed_at", sa.DateTime(timezone=True)),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema=SCHEMA,
    )

    # ── Capability-separated group roles (NOLOGIN; no passwords here) ────
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factory_reader') THEN
                CREATE ROLE factory_reader NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factory_writer') THEN
                CREATE ROLE factory_writer NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factory_gate') THEN
                CREATE ROLE factory_gate NOLOGIN;
            END IF;
        END $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO factory_reader, factory_writer, factory_gate")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO factory_reader, factory_writer, factory_gate")
    op.execute(f"GRANT INSERT ON ALL TABLES IN SCHEMA {SCHEMA} TO factory_writer")
    op.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {SCHEMA} TO factory_writer, factory_gate")
    # Only the gate role decides promotions.
    op.execute(f"REVOKE INSERT ON {SCHEMA}.gate_decisions FROM factory_writer")
    op.execute(f"REVOKE INSERT, UPDATE ON {SCHEMA}.hypothesis_status FROM factory_writer")
    op.execute(f"GRANT INSERT ON {SCHEMA}.gate_decisions, {SCHEMA}.holdout_access_log, "
               f"{SCHEMA}.alpha_wealth_ledger TO factory_gate")
    op.execute(f"GRANT INSERT, UPDATE ON {SCHEMA}.hypothesis_status TO factory_gate")
    # The reader role never sees production writes; it only reads snapshots.
    op.execute("GRANT SELECT ON public.price_points, public.earnings_surprises, "
               "public.corporate_actions, public.news_items, public.impact_reports, "
               "public.expectations, public.companies, public.market_state, "
               "public.paper_trades, public.paper_signal_marks TO factory_reader")


def downgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    # Roles are intentionally NOT dropped: other grants may reference them and
    # dropping a role that owns objects fails. Remove them by hand if needed.

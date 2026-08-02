"""Admin panel substrate: roles, audit, activity, alerts, features, cost log,
paper archives.

Everything here is additive. Six concerns in one reviewable migration:

1. app_users.role ('admin'|'internal'|'external') — replaces the env-var
   INTERNAL_RESEARCH_EMAILS as the privilege axis (env kept as bootstrap
   fallback in api/auth.py). Backfilled from the env at migration runtime;
   ADMIN_EMAILS (default the founder) upserted as admin.
2. admin_audit_log — one row per admin action, always.
3. user_activity_events — unified API-request + page_view + heartbeat log
   (the analytics substrate; nothing per-user was recorded before this).
4. alert_events — durable history for GuardrailMonitor alerts (previously
   in-memory-only, lost on every restart).
5. user_features — per-user feature DISABLE overrides (no row = enabled).
6. llm_call_log — durable promotion of the ephemeral LLM-cost JSONL
   (LLM_LOG_FILE lived on container FS and was wiped on restart — review
   finding U3). History starts at deploy; no backfill possible.
7. paper_*_archive — archive-first paper-ledger reset targets. LIKE without
   the UNIQUE(impact_id,symbol,strategy) key so multiple reset generations
   coexist; every archived row stamped with batch id / reason / actor.

Revision ID: 035
Revises: 034
Create Date: 2026-07-31 00:00:00.000000
"""
import os

from alembic import op
import sqlalchemy as sa


revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Roles ──────────────────────────────────────────────────────────
    op.execute(
        "ALTER TABLE app_users ADD COLUMN role TEXT NOT NULL DEFAULT 'external'"
    )
    op.execute(
        "ALTER TABLE app_users ADD CONSTRAINT ck_app_users_role "
        "CHECK (role IN ('admin','internal','external'))"
    )

    conn = op.get_bind()
    internal = [
        e.strip().lower()
        for e in os.getenv("INTERNAL_RESEARCH_EMAILS", "").split(",")
        if e.strip()
    ]
    if internal:
        conn.execute(
            sa.text(
                "UPDATE app_users SET role = 'internal' "
                "WHERE lower(email) = ANY(:emails)"
            ),
            {"emails": internal},
        )
    admins = [
        e.strip().lower()
        for e in os.getenv("ADMIN_EMAILS", "aviralv35@gmail.com").split(",")
        if e.strip()
    ]
    for email in admins:
        conn.execute(
            sa.text(
                "INSERT INTO app_users (email, is_active, role) "
                "VALUES (:email, true, 'admin') "
                "ON CONFLICT (email) DO UPDATE "
                "SET role = 'admin', is_active = true"
            ),
            {"email": email},
        )

    # ── 2. Admin audit ────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE admin_audit_log (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            actor_email TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            payload JSONB,
            success BOOLEAN NOT NULL DEFAULT true
        )
    """)
    op.execute("CREATE INDEX ix_admin_audit_ts ON admin_audit_log (ts DESC)")

    # ── 3. Activity (API log + frontend beacon, unified) ──────────────────
    # path holds the ROUTE TEMPLATE (/company/{symbol}), never the raw URL.
    op.execute("""
        CREATE TABLE user_activity_events (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            user_id BIGINT,
            email TEXT,
            event_type TEXT NOT NULL,
            method TEXT,
            path TEXT,
            status SMALLINT,
            duration_ms INTEGER,
            feature TEXT,
            CONSTRAINT ck_activity_event_type
                CHECK (event_type IN ('api','page_view','heartbeat'))
        )
    """)
    op.execute(
        "CREATE INDEX ix_activity_user_ts ON user_activity_events (user_id, ts DESC)"
    )
    op.execute("CREATE INDEX ix_activity_ts ON user_activity_events (ts)")

    # ── 4. Alert history ──────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE alert_events (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            service TEXT NOT NULL,
            kind TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT,
            delivered BOOLEAN
        )
    """)
    op.execute("CREATE INDEX ix_alert_events_ts ON alert_events (ts DESC)")
    op.execute("CREATE INDEX ix_alert_events_kind_ts ON alert_events (kind, ts DESC)")

    # ── 5. Per-user feature overrides (no row = enabled) ──────────────────
    op.execute("""
        CREATE TABLE user_features (
            user_id BIGINT NOT NULL
                REFERENCES app_users(id) ON DELETE CASCADE,
            feature TEXT NOT NULL,
            enabled BOOLEAN NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by TEXT NOT NULL,
            PRIMARY KEY (user_id, feature)
        )
    """)

    # ── 6. Durable LLM cost log ───────────────────────────────────────────
    # Mirrors analysis/llm_logger.LLMCallRecord (incl. the 2026-07 fields:
    # thinking/cached tokens, web_search_calls, turns). context_id links
    # analysis-stage calls to their news_id (per-filing cost rollups) and
    # dossier/grounding calls to their symbol.
    op.execute("""
        CREATE TABLE llm_call_log (
            id BIGSERIAL PRIMARY KEY,
            call_id TEXT NOT NULL UNIQUE,
            ts TIMESTAMPTZ NOT NULL,
            service TEXT,
            model TEXT NOT NULL,
            stage TEXT NOT NULL,
            context_id TEXT,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            thinking_tokens INTEGER NOT NULL DEFAULT 0,
            cached_tokens INTEGER NOT NULL DEFAULT 0,
            web_search_calls INTEGER NOT NULL DEFAULT 0,
            turns INTEGER NOT NULL DEFAULT 0,
            latency_ms DOUBLE PRECISION,
            cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
            success BOOLEAN NOT NULL DEFAULT true,
            error TEXT
        )
    """)
    op.execute("CREATE INDEX ix_llm_call_log_ts ON llm_call_log (ts)")
    op.execute("CREATE INDEX ix_llm_call_log_stage_ts ON llm_call_log (stage, ts)")
    op.execute("CREATE INDEX ix_llm_call_log_context ON llm_call_log (context_id)")

    # ── 7. Paper-ledger archives ──────────────────────────────────────────
    for src in ("paper_trades", "paper_equity_snapshots", "paper_signal_marks"):
        op.execute(f"""
            CREATE TABLE {src}_archive
                (LIKE {src} INCLUDING DEFAULTS)
        """)
        op.execute(f"""
            ALTER TABLE {src}_archive
                ADD COLUMN archived_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                ADD COLUMN archive_batch_id TEXT NOT NULL,
                ADD COLUMN archive_reason TEXT NOT NULL,
                ADD COLUMN archived_by TEXT NOT NULL
        """)
        op.execute(
            f"CREATE INDEX ix_{src}_archive_batch "
            f"ON {src}_archive (archive_batch_id)"
        )


def downgrade() -> None:
    for src in ("paper_signal_marks", "paper_equity_snapshots", "paper_trades"):
        op.execute(f"DROP TABLE IF EXISTS {src}_archive")
    op.execute("DROP TABLE IF EXISTS llm_call_log")
    op.execute("DROP TABLE IF EXISTS user_features")
    op.execute("DROP TABLE IF EXISTS alert_events")
    op.execute("DROP TABLE IF EXISTS user_activity_events")
    op.execute("DROP TABLE IF EXISTS admin_audit_log")
    op.execute("ALTER TABLE app_users DROP CONSTRAINT IF EXISTS ck_app_users_role")
    op.execute("ALTER TABLE app_users DROP COLUMN IF EXISTS role")

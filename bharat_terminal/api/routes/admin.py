"""Admin panel API — founder-only control surface.

Mounted in main.py behind require_admin (a fourth tier above _internal) —
deliberately NOT part of db_explorer, which is customer-visible. Every
mutating endpoint follows the same contract:

    do the write  ->  insert an admin_audit_log row  ->  invalidate the
    target user's authz cache (role/feature changes bite instantly).

Conventions:
- Plain-dict responses, raw text() SQL (codebase style; no ORM).
- Destructive operations are DRY-RUN BY DEFAULT with an explicit
  execute=true (the manage.py --execute convention).
- Timestamps are stored UTC everywhere; ONLY display-facing daily
  bucketing converts with AT TIME ZONE 'Asia/Kolkata'. Keep it that way.
- Out-of-band SQL edits to app_users/user_features bypass the 60s authz
  cache — after manual SQL, run: redis-cli DEL authz:user:{id}.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from bharat_terminal.api.auth import (
    FEATURE_REGISTRY,
    invalidate_authz,
    require_admin,
)
from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
EMAIL_WORKFLOW_URL = os.getenv("EMAIL_WORKFLOW_URL", "")
DOCKER_PROXY_URL = os.getenv("DOCKER_PROXY_URL", "http://docker-proxy:2375")
REDPANDA_METRICS_URL = os.getenv(
    "REDPANDA_METRICS_URL", "http://redpanda:9644/public_metrics")

# Services expected to write hb:{name} liveness beats (bharat_terminal/
# heartbeat.py). Static on purpose: a service that STOPS beating must show
# as stalled, which auto-discovery would silently drop instead.
EXPECTED_HEARTBEATS = (
    "analysis", "papertrade", "ingestion", "kb-monitor",
    "sentiment", "commodity", "transcript-watcher", "onboarding",
)

_WINDOWS = {"1h": "1 hour", "24h": "24 hours", "7d": "7 days", "30d": "30 days"}

_redis = None


async def get_redis():
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


_ROLES = ("admin", "internal", "external")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def _audit(
    session,
    actor_email: str,
    action: str,
    target: Optional[str],
    payload: Optional[dict] = None,
    success: bool = True,
) -> None:
    """One row per admin action, in the caller's transaction where possible."""
    await session.execute(
        text(
            "INSERT INTO admin_audit_log (actor_email, action, target, payload, success) "
            "VALUES (:actor, :action, :target, CAST(:payload AS jsonb), :success)"
        ),
        {
            "actor": actor_email,
            "action": action,
            "target": target,
            "payload": json.dumps(payload or {}, default=str),
            "success": success,
        },
    )


# ── Users ───────────────────────────────────────────────────────────────────
@router.get("/users")
async def list_users(admin: dict = Depends(require_admin)):
    factory = get_session_factory()
    async with factory() as s:
        users = (await s.execute(text("""
            SELECT u.id, u.email, u.name, u.picture, u.role, u.is_active,
                   u.invited_at, u.last_login_at, u.created_at,
                   COALESCE(f.disabled, '{}') AS disabled_features
            FROM app_users u
            LEFT JOIN (
                SELECT user_id, array_agg(feature ORDER BY feature) AS disabled
                FROM user_features WHERE enabled = false GROUP BY user_id
            ) f ON f.user_id = u.id
            ORDER BY u.created_at
        """))).mappings().all()
    return {"users": [dict(u) for u in users], "count": len(users)}


class InviteBody(BaseModel):
    email: str
    role: str = "external"
    send_email: bool = True


@router.post("/users")
async def invite_user(body: InviteBody, admin: dict = Depends(require_admin)):
    """Add a user to the invite allowlist (+ optional onboarding email).

    If the email already has a row, NOTHING is changed (no silent
    reactivation — use PATCH for that); the response says so.
    """
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="invalid_email")
    if body.role not in _ROLES:
        raise HTTPException(status_code=422, detail="invalid_role")

    factory = get_session_factory()
    async with factory() as s:
        row = (await s.execute(
            text(
                "INSERT INTO app_users (email, is_active, role) "
                "VALUES (:email, true, :role) "
                "ON CONFLICT (email) DO NOTHING "
                "RETURNING id, email, role, is_active"
            ),
            {"email": email, "role": body.role},
        )).mappings().first()
        created = row is not None
        if not created:
            row = (await s.execute(
                text("SELECT id, email, role, is_active FROM app_users "
                     "WHERE lower(email) = :email"),
                {"email": email},
            )).mappings().first()
        await _audit(s, admin["email"], "user.invite", email,
                     {"role": body.role, "created": created,
                      "send_email": body.send_email})
        await s.commit()

    email_sent = False
    email_error = None
    if created and body.send_email:
        # Reuse the onboarding worker's email builder verbatim (pure function).
        from bharat_terminal.onboarding.provision_worker import _onboarding_email
        if not EMAIL_WORKFLOW_URL:
            email_error = "EMAIL_WORKFLOW_URL not configured"
        else:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        EMAIL_WORKFLOW_URL, json=_onboarding_email(email))
                    resp.raise_for_status()
                email_sent = True
            except Exception as e:  # invite stands even if the mail bounces
                email_error = str(e)[:200]
                logger.warning("onboarding email failed for %s: %s", email, e)

    return {
        "created": created,
        "user": dict(row) if row else None,
        "email_sent": email_sent,
        "email_error": email_error,
    }


class UserPatchBody(BaseModel):
    is_active: Optional[bool] = None
    role: Optional[str] = None


@router.patch("/users/{user_id}")
async def patch_user(user_id: int, body: UserPatchBody,
                     admin: dict = Depends(require_admin)):
    """Activate/deactivate a user or change their role.

    Guard: the last active admin can neither be deactivated nor demoted
    (checked inside the same transaction). Deactivation takes effect on the
    user's next request (authz cache is invalidated here).
    """
    if body.is_active is None and body.role is None:
        raise HTTPException(status_code=422, detail="empty_patch")
    if body.role is not None and body.role not in _ROLES:
        raise HTTPException(status_code=422, detail="invalid_role")

    factory = get_session_factory()
    async with factory() as s:
        current = (await s.execute(
            text("SELECT id, email, role, is_active FROM app_users "
                 "WHERE id = :id FOR UPDATE"),
            {"id": user_id},
        )).mappings().first()
        if current is None:
            raise HTTPException(status_code=404, detail="user_not_found")

        losing_admin = current["role"] == "admin" and (
            body.is_active is False
            or (body.role is not None and body.role != "admin")
        )
        if losing_admin:
            others = (await s.execute(
                text("SELECT count(*) FROM app_users "
                     "WHERE role = 'admin' AND is_active AND id != :id"),
                {"id": user_id},
            )).scalar()
            if not others:
                raise HTTPException(status_code=409, detail="last_active_admin")

        sets, params = [], {"id": user_id}
        if body.is_active is not None:
            sets.append("is_active = :is_active")
            params["is_active"] = body.is_active
        if body.role is not None:
            sets.append("role = :role")
            params["role"] = body.role
        row = (await s.execute(
            text(f"UPDATE app_users SET {', '.join(sets)} WHERE id = :id "
                 "RETURNING id, email, role, is_active"),
            params,
        )).mappings().first()
        await _audit(s, admin["email"], "user.patch", current["email"],
                     {"from": {"role": current["role"],
                               "is_active": current["is_active"]},
                      "to": body.model_dump(exclude_none=True)})
        await s.commit()

    await invalidate_authz(user_id)
    return {"user": dict(row)}


# ── Feature flags ───────────────────────────────────────────────────────────
@router.get("/features")
async def list_features(admin: dict = Depends(require_admin)):
    factory = get_session_factory()
    async with factory() as s:
        overrides = (await s.execute(text("""
            SELECT f.user_id, u.email, f.feature, f.enabled,
                   f.updated_at, f.updated_by
            FROM user_features f JOIN app_users u ON u.id = f.user_id
            ORDER BY u.email, f.feature
        """))).mappings().all()
    return {
        "registry": {k: v for k, v in FEATURE_REGISTRY.items()},
        "overrides": [dict(o) for o in overrides],
    }


class FeatureBody(BaseModel):
    feature: str
    enabled: bool


@router.put("/users/{user_id}/features")
async def set_user_feature(user_id: int, body: FeatureBody,
                           admin: dict = Depends(require_admin)):
    """Set one feature override. enabled=true deletes the override row
    (no-row = enabled is the invariant — the table stores only disables)."""
    if body.feature not in FEATURE_REGISTRY:
        raise HTTPException(status_code=422, detail="unknown_feature")

    factory = get_session_factory()
    async with factory() as s:
        target = (await s.execute(
            text("SELECT email FROM app_users WHERE id = :id"), {"id": user_id},
        )).mappings().first()
        if target is None:
            raise HTTPException(status_code=404, detail="user_not_found")
        if body.enabled:
            await s.execute(
                text("DELETE FROM user_features "
                     "WHERE user_id = :id AND feature = :feature"),
                {"id": user_id, "feature": body.feature},
            )
        else:
            await s.execute(
                text("""
                    INSERT INTO user_features (user_id, feature, enabled, updated_by)
                    VALUES (:id, :feature, false, :actor)
                    ON CONFLICT (user_id, feature) DO UPDATE
                    SET enabled = false, updated_at = now(), updated_by = :actor
                """),
                {"id": user_id, "feature": body.feature, "actor": admin["email"]},
            )
        await _audit(s, admin["email"], "feature.set", target["email"],
                     {"feature": body.feature, "enabled": body.enabled})
        await s.commit()

    await invalidate_authz(user_id)
    return {"user_id": user_id, "feature": body.feature, "enabled": body.enabled}


# ── Audit log ───────────────────────────────────────────────────────────────
@router.get("/audit-log")
async def audit_log(limit: int = Query(100, le=500), offset: int = Query(0, ge=0),
                    admin: dict = Depends(require_admin)):
    factory = get_session_factory()
    async with factory() as s:
        rows = (await s.execute(
            text("SELECT id, ts, actor_email, action, target, payload, success "
                 "FROM admin_audit_log ORDER BY ts DESC "
                 "LIMIT :limit OFFSET :offset"),
            {"limit": limit, "offset": offset},
        )).mappings().all()
    return {"entries": [dict(r) for r in rows]}


# ── Paper-ledger reset ──────────────────────────────────────────────────────
_PAPER_TABLES = ("paper_trades", "paper_equity_snapshots", "paper_signal_marks")


class PaperResetBody(BaseModel):
    execute: bool = False  # dry-run by default (manage.py convention)
    reason: str = ""


@router.post("/paper/reset")
async def paper_reset(body: PaperResetBody, admin: dict = Depends(require_admin)):
    """Archive-first reset of the paper-trading ledger.

    Dry-run (default): per-table counts + status breakdown, zero writes.
    Execute: ONE transaction — copy every row into its *_archive table
    stamped with (batch_id, reason, actor), then delete the live rows.

    Safe while the papertrade service runs: the engine keeps no in-memory
    positions (reconcile_on_boot only logs; every loop re-reads the DB), and
    Kafka offsets are untouched (auto_offset_reset=latest) so consumed
    impacts do not re-open. Restarting bharat-papertrade afterwards is
    cosmetic (clears the in-memory t5/t15/t30/t60 _mark_queue).

    This replaces the undocumented manual wipes that left unstamped
    exit_reason='reset' rows inside the pre-registered pilot dataset.
    """
    factory = get_session_factory()
    async with factory() as s:
        counts = {}
        for t in _PAPER_TABLES:
            counts[t] = (await s.execute(text(f"SELECT count(*) FROM {t}"))).scalar()
        status_breakdown = {
            r["status"]: r["n"]
            for r in (await s.execute(text(
                "SELECT status, count(*) AS n FROM paper_trades GROUP BY status"
            ))).mappings()
        }

        if not body.execute:
            return {
                "dry_run": True,
                "counts": counts,
                "paper_trades_by_status": status_breakdown,
                "note": "POST again with execute=true and a reason to archive"
                        " + wipe. Restart bharat-papertrade afterwards"
                        " (cosmetic; clears the in-memory mark queue).",
            }

        reason = body.reason.strip()
        if not reason:
            raise HTTPException(status_code=422, detail="reason_required")

        actor = admin["email"]
        batch_id = (
            f"reset-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f"-{actor.split('@')[0]}"
        )
        archived = {}
        for t in _PAPER_TABLES:
            res = await s.execute(
                text(f"""
                    INSERT INTO {t}_archive
                    SELECT src.*, now(), :batch_id, :reason, :actor
                    FROM {t} src
                """),
                {"batch_id": batch_id, "reason": reason, "actor": actor},
            )
            archived[t] = res.rowcount
            await s.execute(text(f"DELETE FROM {t}"))
        await _audit(s, actor, "paper.reset", "paper_ledger",
                     {"batch_id": batch_id, "reason": reason,
                      "archived": archived,
                      "paper_trades_by_status": status_breakdown})
        await s.commit()

    logger.warning("[ADMIN] paper ledger reset by %s: batch=%s archived=%s",
                   admin["email"], batch_id, archived)
    return {
        "dry_run": False,
        "batch_id": batch_id,
        "archived": archived,
        "restart_hint": "docker restart bharat-papertrade  # cosmetic: clears "
                        "in-memory mark queue; engine state lives in the DB",
    }


# ── Health (containers + loop liveness + kafka lag) ─────────────────────────
async def _docker_containers() -> list[dict]:
    """Container status via the read-only docker-socket-proxy. Empty list +
    logged warning if the proxy is absent (it ships in the same deploy)."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{DOCKER_PROXY_URL}/containers/json",
                                    params={"all": "true"})
            resp.raise_for_status()
            out = []
            for c in resp.json():
                name = (c.get("Names") or ["?"])[0].lstrip("/")
                status = c.get("Status") or ""
                health = None
                if "(healthy" in status:
                    health = "healthy"
                elif "(unhealthy" in status:
                    health = "unhealthy"
                out.append({
                    "name": name,
                    "image": c.get("Image"),
                    "state": c.get("State"),      # running|exited|restarting…
                    "status": status,              # "Up 3 hours (healthy)"
                    "health": health,
                })
            return sorted(out, key=lambda c: c["name"])
    except Exception as e:
        logger.warning("docker-proxy unreachable: %s", str(e)[:200])
        return []


async def _heartbeats() -> list[dict]:
    r = await get_redis()
    now = datetime.now(timezone.utc)
    out = []
    for svc in EXPECTED_HEARTBEATS:
        entry = {"service": svc, "alive": False, "last_beat": None,
                 "age_seconds": None}
        try:
            val = await r.get(f"hb:{svc}")
            if val:
                beat_at = datetime.fromisoformat(val)
                entry.update({
                    "alive": True,
                    "last_beat": val,
                    "age_seconds": round((now - beat_at).total_seconds(), 1),
                })
        except Exception as e:
            entry["error"] = str(e)[:120]
        out.append(entry)
    return out


_LAG_LINE = re.compile(
    r'consumer_group_lag[^{]*\{([^}]*)\}\s+([0-9.eE+-]+)')
_GROUP_LABEL = re.compile(r'redpanda_group="([^"]+)"|group="([^"]+)"')


async def _kafka_lag() -> dict:
    """Per-consumer-group lag parsed from redpanda's public_metrics — the one
    metrics endpoint verified to work in this stack. Fail-soft to {}."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(REDPANDA_METRICS_URL)
            resp.raise_for_status()
        lags: dict[str, float] = {}
        for m in _LAG_LINE.finditer(resp.text):
            labels, value = m.group(1), m.group(2)
            if "_max" in m.group(0).split("{")[0]:
                continue  # keep only the _sum series
            g = _GROUP_LABEL.search(labels)
            group = (g.group(1) or g.group(2)) if g else None
            if group:
                lags[group] = lags.get(group, 0.0) + float(value)
        return {k: int(v) for k, v in lags.items()}
    except Exception as e:
        logger.warning("redpanda metrics unreachable: %s", str(e)[:200])
        return {}


@router.get("/health")
async def admin_health(admin: dict = Depends(require_admin)):
    """Three independent views, composed: container state (docker), loop
    liveness (hb:* — distinguishes 'container Up' from 'loop alive', the
    system's characteristic failure mode), and Kafka consumer-group lag."""
    import asyncio
    containers, beats, lag = await asyncio.gather(
        _docker_containers(), _heartbeats(), _kafka_lag())
    stalled = [b["service"] for b in beats if not b["alive"]]
    return {
        "containers": containers,
        "heartbeats": beats,
        "kafka_lag": lag,
        "stalled_services": stalled,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "heartbeats require the Phase-2 worker rebuild; a service "
                "shows stalled until its rebuilt container beats.",
    }


# ── Alert history ───────────────────────────────────────────────────────────
@router.get("/alerts")
async def list_alerts(
    kind: Optional[str] = None,
    service: Optional[str] = None,
    since_hours: int = Query(168, ge=1, le=24 * 365),
    limit: int = Query(200, le=1000),
    admin: dict = Depends(require_admin),
):
    where = ["ts >= now() - make_interval(hours => :hours)"]
    params: dict = {"hours": since_hours, "limit": limit}
    if kind:
        where.append("kind = :kind")
        params["kind"] = kind
    if service:
        where.append("service = :service")
        params["service"] = service
    factory = get_session_factory()
    async with factory() as s:
        rows = (await s.execute(
            text(f"""
                SELECT id, ts, service, kind, subject, body, delivered
                FROM alert_events WHERE {' AND '.join(where)}
                ORDER BY ts DESC LIMIT :limit
            """), params)).mappings().all()
        kinds = (await s.execute(
            text("""
                SELECT kind, count(*) AS n, max(ts) AS latest
                FROM alert_events
                WHERE ts >= now() - make_interval(hours => :hours)
                GROUP BY kind ORDER BY n DESC
            """), {"hours": since_hours})).mappings().all()
    return {"alerts": [dict(r) for r in rows],
            "by_kind": [dict(k) for k in kinds]}


# ── Latency (p50/p90/p99 per module) ───────────────────────────────────────
_PCTS = "percentile_cont(ARRAY[0.5, 0.9, 0.99]) WITHIN GROUP (ORDER BY {col})"


def _pct_row(row) -> Optional[dict]:
    if not row or row["n"] == 0 or row["p"] is None:
        return None
    p50, p90, p99 = row["p"]
    return {"n": row["n"], "p50_ms": round(p50, 1), "p90_ms": round(p90, 1),
            "p99_ms": round(p99, 1)}


@router.get("/latency")
async def latency_summary(window: str = Query("24h"),
                          admin: dict = Depends(require_admin)):
    if window not in _WINDOWS:
        raise HTTPException(status_code=422, detail="bad_window")
    iv = _WINDOWS[window]
    factory = get_session_factory()
    out: dict = {"window": window, "modules": {}}
    async with factory() as s:
        async def one(name: str, sql: str, params: Optional[dict] = None):
            try:
                row = (await s.execute(text(sql), params or {})).mappings().first()
                out["modules"][name] = _pct_row(row)
            except Exception as e:
                await s.rollback()
                out["modules"][name] = {"error": str(e)[:150]}

        pcts = _PCTS.format(col="processing_latency_ms")
        await one("pipeline_end_to_end", f"""
            SELECT count(*) AS n, {pcts} AS p FROM impact_reports
            WHERE created_at >= now() - interval '{iv}'
              AND processing_latency_ms IS NOT NULL""")
        await one("analysis_audited", f"""
            SELECT count(*) AS n, {pcts} AS p FROM analysis_audit
            WHERE analysis_created_at >= now() - interval '{iv}'
              AND processing_latency_ms IS NOT NULL""")
        pcts = _PCTS.format(col="ingest_latency_ms")
        await one("ingestion", f"""
            SELECT count(*) AS n, {pcts} AS p FROM news_items
            WHERE created_at >= now() - interval '{iv}'
              AND ingest_latency_ms IS NOT NULL""")
        pcts = _PCTS.format(col="latency_ms")
        await one("research_chat", f"""
            SELECT count(*) AS n, {pcts} AS p FROM research_chat_log
            WHERE asked_at >= now() - interval '{iv}'""")
        await one("api_requests", f"""
            SELECT count(*) AS n, {pcts} AS p FROM user_activity_events
            WHERE event_type = 'api' AND ts >= now() - interval '{iv}'
              AND duration_ms IS NOT NULL""")

        # LLM per stage (durable llm_call_log; empty until Phase-2 rebuild).
        try:
            rows = (await s.execute(text(f"""
                SELECT stage, count(*) AS n, {pcts} AS p,
                       round(avg(latency_ms)::numeric, 1) AS mean_ms
                FROM llm_call_log
                WHERE ts >= now() - interval '{iv}'
                GROUP BY stage ORDER BY n DESC"""))).mappings().all()
            out["llm_stages"] = [
                {**({"stage": r["stage"], "mean_ms": float(r["mean_ms"] or 0)}),
                 **(_pct_row(r) or {})} for r in rows]
        except Exception as e:
            await s.rollback()
            out["llm_stages"] = {"error": str(e)[:150]}

        # Sync jobs: duration derived from sync_log (partial coverage — only
        # jobs that write sync_log rows; several kb-monitor loops do not).
        try:
            rows = (await s.execute(text(f"""
                SELECT sync_type, source, count(*) AS runs,
                       count(*) FILTER (WHERE status != 'success'
                                        AND status != 'running') AS failed,
                       round(avg(EXTRACT(EPOCH FROM (completed_at - started_at))
                            )::numeric, 1) AS mean_s,
                       max(started_at) AS last_run
                FROM sync_log
                WHERE started_at >= now() - interval '{iv}'
                GROUP BY sync_type, source ORDER BY last_run DESC"""))).mappings().all()
            out["sync_jobs"] = [dict(r) for r in rows]
        except Exception as e:
            await s.rollback()
            out["sync_jobs"] = {"error": str(e)[:150]}
    return out


# ── Costs (LLM spend + per-filing economics) ───────────────────────────────
# Stage sets for per-unit rollups. batch_analysis is deliberately excluded
# from per-filing stats (one call spans many filings; context_id is empty).
_FILING_STAGES = ("stage2_extraction", "stage3_impact", "stage5_signals",
                  "filing_summary")


@router.get("/costs")
async def cost_summary(window: str = Query("mtd"),
                       admin: dict = Depends(require_admin)):
    """LLM spend. window=mtd (calendar month, IST boundary) | 1h/24h/7d/30d.

    Totals = llm_call_log + research_chat_log (the chat table predates the
    durable log and stays authoritative for chat spend). llm_call_log history
    starts at the Phase-2 deploy — the first month is partial; the response
    carries data_since so the UI can label it.
    """
    factory = get_session_factory()
    if window == "mtd":
        # First instant of the current IST month, expressed back in UTC.
        time_pred = ("ts >= (date_trunc('month', now() AT TIME ZONE "
                     "'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata')")
        chat_pred = time_pred.replace("ts >=", "asked_at >=")
    elif window in _WINDOWS:
        time_pred = f"ts >= now() - interval '{_WINDOWS[window]}'"
        chat_pred = f"asked_at >= now() - interval '{_WINDOWS[window]}'"
    else:
        raise HTTPException(status_code=422, detail="bad_window")

    out: dict = {"window": window}
    factory = get_session_factory()
    async with factory() as s:
        try:
            totals = (await s.execute(text(f"""
                SELECT coalesce(sum(cost_usd), 0) AS llm_usd,
                       count(*) AS calls,
                       coalesce(sum(cost_usd) FILTER (WHERE NOT success), 0)
                           AS failed_usd,
                       count(*) FILTER (WHERE NOT success) AS failed_calls,
                       coalesce(sum(web_search_calls), 0) AS web_searches,
                       min(ts) AS data_since
                FROM llm_call_log WHERE {time_pred}"""))).mappings().first()
            chat = (await s.execute(text(f"""
                SELECT coalesce(sum(cost_usd), 0) AS usd, count(*) AS asks
                FROM research_chat_log WHERE {chat_pred}"""))).mappings().first()
            out["total_usd"] = round(float(totals["llm_usd"]) + float(chat["usd"]), 4)
            out["llm"] = {
                "usd": round(float(totals["llm_usd"]), 4),
                "calls": totals["calls"],
                "failed_usd": round(float(totals["failed_usd"]), 4),
                "failed_calls": totals["failed_calls"],
                "web_search_calls": int(totals["web_searches"]),
                "data_since": totals["data_since"],
            }
            out["research_chat"] = {"usd": round(float(chat["usd"]), 4),
                                    "asks": chat["asks"]}

            for dim in ("stage", "model", "service"):
                rows = (await s.execute(text(f"""
                    SELECT {dim} AS k, count(*) AS calls,
                           round(sum(cost_usd)::numeric, 4) AS usd,
                           sum(prompt_tokens) AS tok_in,
                           sum(completion_tokens) AS tok_out
                    FROM llm_call_log WHERE {time_pred}
                    GROUP BY {dim} ORDER BY sum(cost_usd) DESC"""))).mappings().all()
                out[f"by_{dim}"] = [dict(r) for r in rows]

            # Daily series — DISPLAY bucketing in IST (storage stays UTC).
            rows = (await s.execute(text(f"""
                SELECT (ts AT TIME ZONE 'Asia/Kolkata')::date AS day,
                       round(sum(cost_usd)::numeric, 4) AS usd,
                       count(*) AS calls
                FROM llm_call_log WHERE {time_pred}
                GROUP BY 1 ORDER BY 1"""))).mappings().all()
            out["daily"] = [dict(r) for r in rows]

            # Per-filing cost: sum of a filing's analysis-stage calls.
            stages = ", ".join(f"'{st}'" for st in _FILING_STAGES)
            row = (await s.execute(text(f"""
                WITH per_filing AS (
                    SELECT context_id, sum(cost_usd) AS c
                    FROM llm_call_log
                    WHERE {time_pred} AND stage IN ({stages})
                      AND context_id IS NOT NULL
                    GROUP BY context_id)
                SELECT count(*) AS filings,
                       round(avg(c)::numeric, 5) AS mean_usd,
                       round((percentile_cont(0.5) WITHIN GROUP
                              (ORDER BY c))::numeric, 5) AS median_usd,
                       round((percentile_cont(0.9) WITHIN GROUP
                              (ORDER BY c))::numeric, 5) AS p90_usd
                FROM per_filing"""))).mappings().first()
            out["per_filing"] = dict(row) if row else None

            # Per-dossier cost (agent loop rows; grounding priced separately
            # under by_stage['dossier_grounding'] — do not double-count).
            row = (await s.execute(text(f"""
                WITH per_dossier AS (
                    SELECT context_id, sum(cost_usd) AS c
                    FROM llm_call_log
                    WHERE {time_pred} AND stage = 'dossier'
                      AND context_id IS NOT NULL
                    GROUP BY context_id)
                SELECT count(*) AS dossiers,
                       round(avg(c)::numeric, 5) AS mean_usd,
                       round((percentile_cont(0.5) WITHIN GROUP
                              (ORDER BY c))::numeric, 5) AS median_usd
                FROM per_dossier"""))).mappings().first()
            out["per_dossier"] = dict(row) if row else None
        except Exception as e:
            await s.rollback()
            # Pre-migration-035: report absence, don't 500 (kb guidance
            # endpoint precedent).
            logger.warning("cost tables unavailable: %s", e)
            out["error"] = "llm_call_log unavailable (pre-migration-035?)"
    return out


# ── User analytics ──────────────────────────────────────────────────────────
# Sessionization: page_view + heartbeat events, a >5-min gap starts a new
# session, +60s tail per session (the final heartbeat covers ~1 more minute).
_SESSION_SQL = """
    WITH e AS (
        SELECT user_id, ts,
               CASE WHEN ts - lag(ts) OVER (PARTITION BY user_id ORDER BY ts)
                         > interval '5 minutes'
                    OR lag(ts) OVER (PARTITION BY user_id ORDER BY ts) IS NULL
               THEN 1 ELSE 0 END AS new_session
        FROM user_activity_events
        WHERE event_type IN ('page_view', 'heartbeat')
          AND ts >= now() - make_interval(days => :days)
          {user_pred}
    ), s AS (
        SELECT user_id, ts,
               sum(new_session) OVER (PARTITION BY user_id ORDER BY ts)
                   AS session_id
        FROM e
    ), sessions AS (
        SELECT user_id, session_id, min(ts) AS started,
               EXTRACT(EPOCH FROM (max(ts) - min(ts))) + 60 AS seconds
        FROM s GROUP BY user_id, session_id
    )
    SELECT user_id, count(*) AS visits,
           round(sum(seconds)::numeric, 0) AS total_seconds,
           max(started) AS last_visit
    FROM sessions GROUP BY user_id
"""


@router.get("/analytics/users")
async def analytics_users(days: int = Query(30, ge=1, le=365),
                          admin: dict = Depends(require_admin)):
    """Per-user engagement over the window: visits (sessionized), total time,
    last seen, top features by API activity. Empty until the beacon +
    middleware deploy starts populating user_activity_events."""
    factory = get_session_factory()
    out: dict = {"days": days, "users": []}
    async with factory() as s:
        try:
            base = (await s.execute(text("""
                SELECT id, email, name, role, is_active, last_login_at
                FROM app_users ORDER BY email"""))).mappings().all()
            sessions = {
                r["user_id"]: r for r in (await s.execute(
                    text(_SESSION_SQL.format(user_pred="")), {"days": days},
                )).mappings()
            }
            feats = (await s.execute(text("""
                SELECT user_id, feature, count(*) AS n
                FROM user_activity_events
                WHERE event_type = 'api' AND feature IS NOT NULL
                  AND ts >= now() - make_interval(days => :days)
                GROUP BY user_id, feature"""), {"days": days})).mappings().all()
            views = {
                r["user_id"]: r["n"] for r in (await s.execute(text("""
                    SELECT user_id, count(*) AS n FROM user_activity_events
                    WHERE event_type = 'page_view'
                      AND ts >= now() - make_interval(days => :days)
                    GROUP BY user_id"""), {"days": days})).mappings()
            }
        except Exception as e:
            await s.rollback()
            logger.warning("analytics tables unavailable: %s", e)
            return {"days": days, "users": [],
                    "error": "user_activity_events unavailable "
                             "(pre-migration-035?)"}

    top_feats: dict[int, list] = {}
    for r in feats:
        top_feats.setdefault(r["user_id"], []).append((r["feature"], r["n"]))
    for lst in top_feats.values():
        lst.sort(key=lambda fn: -fn[1])

    for u in base:
        sess = sessions.get(u["id"])
        out["users"].append({
            **dict(u),
            "visits": int(sess["visits"]) if sess else 0,
            "total_seconds": int(sess["total_seconds"]) if sess else 0,
            "last_visit": sess["last_visit"] if sess else None,
            "page_views": int(views.get(u["id"], 0)),
            "top_features": [
                {"feature": f, "n": n}
                for f, n in top_feats.get(u["id"], [])[:3]
            ],
        })
    return out


@router.get("/analytics/users/{user_id}")
async def analytics_user_detail(user_id: int,
                                days: int = Query(30, ge=1, le=365),
                                admin: dict = Depends(require_admin)):
    """Drill-in: daily activity series (IST display bucketing), feature
    histogram, page views, plus cross-store context — Redis watchlist,
    starred count, research-chat spend (keyed by email, table convention)."""
    factory = get_session_factory()
    async with factory() as s:
        user = (await s.execute(
            text("SELECT id, email, name, role, is_active, last_login_at, "
                 "invited_at FROM app_users WHERE id = :id"),
            {"id": user_id})).mappings().first()
        if user is None:
            raise HTTPException(status_code=404, detail="user_not_found")

        daily, features, pages, sessions = [], [], [], None
        try:
            # DISPLAY bucketing in IST; storage stays UTC (file convention).
            daily = [dict(r) for r in (await s.execute(text("""
                SELECT (ts AT TIME ZONE 'Asia/Kolkata')::date AS day,
                       count(*) FILTER (WHERE event_type = 'api') AS api_calls,
                       count(*) FILTER (WHERE event_type = 'page_view') AS page_views,
                       count(*) FILTER (WHERE event_type = 'heartbeat') AS heartbeats
                FROM user_activity_events
                WHERE user_id = :id AND ts >= now() - make_interval(days => :days)
                GROUP BY 1 ORDER BY 1"""),
                {"id": user_id, "days": days})).mappings()]
            features = [dict(r) for r in (await s.execute(text("""
                SELECT feature, count(*) AS n,
                       round(avg(duration_ms)::numeric, 0) AS avg_ms
                FROM user_activity_events
                WHERE user_id = :id AND event_type = 'api' AND feature IS NOT NULL
                  AND ts >= now() - make_interval(days => :days)
                GROUP BY feature ORDER BY n DESC"""),
                {"id": user_id, "days": days})).mappings()]
            pages = [dict(r) for r in (await s.execute(text("""
                SELECT path, count(*) AS n, max(ts) AS last_seen
                FROM user_activity_events
                WHERE user_id = :id AND event_type = 'page_view'
                  AND ts >= now() - make_interval(days => :days)
                GROUP BY path ORDER BY n DESC LIMIT 30"""),
                {"id": user_id, "days": days})).mappings()]
            sessions = (await s.execute(
                text(_SESSION_SQL.format(user_pred="AND user_id = :uid")),
                {"days": days, "uid": user_id})).mappings().first()
        except Exception as e:
            await s.rollback()
            logger.warning("analytics tables unavailable: %s", e)

        starred = 0
        try:
            starred = (await s.execute(
                text("SELECT count(*) FROM starred_events WHERE user_id = :id"),
                {"id": user_id})).scalar() or 0
        except Exception:
            await s.rollback()

        chat = None
        try:
            chat = dict((await s.execute(text("""
                SELECT count(*) AS asks,
                       round(coalesce(sum(cost_usd), 0)::numeric, 4) AS usd
                FROM research_chat_log
                WHERE lower(user_email) = lower(:email)
                  AND asked_at >= now() - make_interval(days => :days)"""),
                {"email": user["email"], "days": days})).mappings().first())
        except Exception:
            await s.rollback()

    # Watchlist lives ONLY in Redis (routes/watchlist.py blob) — read directly.
    watchlist = []
    try:
        r = await get_redis()
        raw = await r.get(f"watchlist:user:{user_id}")
        if raw:
            watchlist = json.loads(raw)
    except Exception as e:
        logger.warning("watchlist read failed for %s: %s", user_id, e)

    return {
        "user": dict(user),
        "days": days,
        "visits": int(sessions["visits"]) if sessions else 0,
        "total_seconds": int(sessions["total_seconds"]) if sessions else 0,
        "daily": daily,
        "features": features,
        "pages": pages,
        "watchlist": watchlist,
        "starred_count": int(starred),
        "research_chat": chat,
    }

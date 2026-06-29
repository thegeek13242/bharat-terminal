"""
Pipeline guardrails: processing-time budgets, cost budgets, and breach-driven
email alerting.

A single stuck or runaway item must not block the consumer or silently drain
credits. This module provides:
  - time budgets, enforced at the call site via asyncio.wait_for (a too-slow
    item/batch is aborted, never allowed to monopolize the consumer)
  - a rolling 1-hour LLM-cost tracker that flags when spend exceeds budget
  - per-item cost-budget breach recording
  - a breach tracker that sends an alert email when a breach type recurs
    CONSISTENTLY (>= N times within a window), with a cooldown so a sustained
    problem produces one email, not a flood

Email is generic SMTP, env-driven. If SMTP isn't configured the alerter logs
the alert at ERROR instead of sending — the pipeline is never affected either
way (alerting is best-effort and fire-and-forget).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import smtplib
import socket
import ssl
import time
import urllib.request
from collections import defaultdict, deque
from email.message import EmailMessage
from typing import Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── time budgets (enforced by the caller via asyncio.wait_for) ───────────────
ITEM_PROCESSING_TIMEOUT_S = float(os.getenv("ITEM_PROCESSING_TIMEOUT_S", "90"))
BATCH_PROCESSING_TIMEOUT_S = float(os.getenv("BATCH_PROCESSING_TIMEOUT_S", "300"))

# ── cost budgets ─────────────────────────────────────────────────────────────
ITEM_COST_BUDGET_USD = float(os.getenv("ITEM_COST_BUDGET_USD", "0.10"))
HOURLY_COST_BUDGET_USD = float(os.getenv("HOURLY_COST_BUDGET_USD", "5.0"))

# ── stall / lag watchdog (critical, one-shot alerts) ─────────────────────────
WATCHDOG_INTERVAL_S = float(os.getenv("WATCHDOG_INTERVAL_S", "60"))
# Stall = work is pending (lag >= threshold) but nothing has been published for
# this long. Distinguishes a stuck consumer from a quiet, idle period.
STALL_NO_PUBLISH_MIN = float(os.getenv("STALL_NO_PUBLISH_MIN", "15"))
STALL_LAG_THRESHOLD = int(os.getenv("STALL_LAG_THRESHOLD", "20"))
# Even while moving, lag this high means we can't keep up / are nearing the
# topic's retention window → data-loss risk.
CONSUMER_LAG_ALERT = int(os.getenv("CONSUMER_LAG_ALERT", "500"))

# ── breach → alert policy ────────────────────────────────────────────────────
# "Consistent" = at least ALERT_BREACH_COUNT breaches of one kind within
# ALERT_BREACH_WINDOW. After an alert fires, the same kind stays quiet for
# ALERT_COOLDOWN so a sustained issue does not spam the inbox.
ALERT_BREACH_WINDOW_S = float(os.getenv("ALERT_BREACH_WINDOW_MIN", "15")) * 60
ALERT_BREACH_COUNT = int(os.getenv("ALERT_BREACH_COUNT", "10"))
ALERT_COOLDOWN_S = float(os.getenv("ALERT_COOLDOWN_MIN", "60")) * 60

# ── Webhook channel (Teams / Slack / Discord — easiest, no auth) ─────────────
# Just paste an incoming-webhook URL. Alerts POST a single JSON payload; no
# credentials, no tenant config. Preferred over SMTP for that reason.
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
# Payload shape: teams (MessageCard) | slack | discord | generic ({"text": ...})
ALERT_WEBHOOK_TYPE = os.getenv("ALERT_WEBHOOK_TYPE", "teams").lower()

# ── SMTP (generic; logs-only if unset) ───────────────────────────────────────
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM", "") or SMTP_USERNAME
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")


class GuardrailMonitor:
    """Process-wide tracker for cost and breach events + the email alerter."""

    def __init__(self) -> None:
        # maxlen bounds memory if a tight error loop floods record_breach (e.g. a
        # poison-pill consumer spinning thousands/sec). The threshold check only
        # needs "enough in the window", so capping the tail is harmless.
        self._breaches: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=10000))
        self._cost_window: Deque[Tuple[float, float]] = deque(maxlen=200000)
        self._last_alert: Dict[str, float] = {}

    # ── rolling cost ─────────────────────────────────────────────────────────
    def record_cost(self, usd: float) -> None:
        """Record one LLM call's cost; flag a breach if the trailing-hour spend
        exceeds the budget. Called for every LLM call."""
        if usd <= 0:
            return
        now = time.time()
        self._cost_window.append((now, usd))
        cutoff = now - 3600
        while self._cost_window and self._cost_window[0][0] < cutoff:
            self._cost_window.popleft()
        hourly = sum(c for _, c in self._cost_window)
        if hourly > HOURLY_COST_BUDGET_USD:
            self.record_breach(
                "HOURLY_COST_BUDGET",
                f"trailing-hour LLM spend ${hourly:.2f} exceeds budget ${HOURLY_COST_BUDGET_USD:.2f}",
            )

    def hourly_cost(self) -> float:
        cutoff = time.time() - 3600
        return sum(c for ts, c in self._cost_window if ts >= cutoff)

    # ── breach recording + alerting ──────────────────────────────────────────
    def record_breach(self, kind: str, detail: str) -> None:
        now = time.time()
        dq = self._breaches[kind]
        dq.append(now)
        cutoff = now - ALERT_BREACH_WINDOW_S
        while dq and dq[0] < cutoff:
            dq.popleft()
        logger.warning(
            "[guardrail] breach %s: %s (%d in last %.0fmin)",
            kind, detail, len(dq), ALERT_BREACH_WINDOW_S / 60,
        )
        if len(dq) >= ALERT_BREACH_COUNT:
            self._maybe_alert(kind, detail, len(dq))

    def _maybe_alert(self, kind: str, detail: str, count: int) -> None:
        win_min = ALERT_BREACH_WINDOW_S / 60
        subject = f"[Bharat Terminal] ALERT: {kind} breached {count}x in {win_min:.0f}min"
        body = (
            "A guardrail in the analysis pipeline is being breached consistently.\n\n"
            f"Breach type : {kind}\n"
            f"Occurrences : {count} within the last {win_min:.0f} minutes\n"
            f"Latest      : {detail}\n"
            f"Host        : {socket.gethostname()}\n"
            f"Hourly spend: ${self.hourly_cost():.2f} (budget ${HOURLY_COST_BUDGET_USD:.2f})\n\n"
            f"Suppressing repeats of this alert for {ALERT_COOLDOWN_S / 60:.0f} minutes.\n"
        )
        self._emit(kind, subject, body)

    def alert_now(self, kind: str, detail: str) -> None:
        """Send an immediate one-shot CRITICAL alert (cooldown-protected).

        For conditions that warrant attention on first detection — pipeline
        stall, provider outage, dependency down — rather than a rate of breaches.
        Safe to call from any service that imports this module.
        """
        subject = f"[Bharat Terminal] CRITICAL: {kind}"
        body = (
            "A critical condition needs immediate attention.\n\n"
            f"Type   : {kind}\n"
            f"Detail : {detail}\n"
            f"Host   : {socket.gethostname()}\n\n"
            f"Suppressing repeats of this alert for {ALERT_COOLDOWN_S / 60:.0f} minutes.\n"
        )
        self._emit(kind, subject, body)

    def _emit(self, kind: str, subject: str, body: str) -> None:
        """Cooldown-gated dispatch shared by rate-based and one-shot alerts."""
        now = time.time()
        if now - self._last_alert.get(kind, 0.0) < ALERT_COOLDOWN_S:
            return  # within cooldown — suppress
        self._last_alert[kind] = now
        # Never let alert delivery block the pipeline: fire-and-forget on the loop
        # if one is running, else send inline (e.g. when called from a worker thread).
        try:
            asyncio.get_running_loop().create_task(self._dispatch_async(subject, body))
        except RuntimeError:
            self._dispatch_alert(subject, body)

    async def _dispatch_async(self, subject: str, body: str) -> None:
        await asyncio.to_thread(self._dispatch_alert, subject, body)

    def _dispatch_alert(self, subject: str, body: str) -> None:
        """Send via every configured channel (webhook preferred — no auth).
        Logs at ERROR if no channel is configured or all fail."""
        sent = False
        if ALERT_WEBHOOK_URL:
            sent = self._send_webhook(subject, body) or sent
        if SMTP_HOST and ALERT_EMAIL_TO and ALERT_EMAIL_FROM:
            sent = self._send_email(subject, body) or sent
        if not sent:
            logger.error(
                "[guardrail] ALERT (no delivery channel configured/working — set "
                "ALERT_WEBHOOK_URL for Teams/Slack, or SMTP_*): %s", subject)

    def _send_webhook(self, subject: str, body: str) -> bool:
        """POST the alert to a Teams/Slack/Discord incoming webhook. Returns
        True on a 2xx response."""
        text = f"{subject}\n\n{body}"
        if ALERT_WEBHOOK_TYPE in ("teams_workflow", "workflow"):
            # Teams "Workflows" incoming webhook: the Post-card action requires a
            # proper Adaptive Card wrapped in a message/attachments envelope.
            payload = {
                "type": "message",
                "attachments": [{
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {"type": "TextBlock", "size": "Medium", "weight": "Bolder",
                             "color": "Attention", "text": subject, "wrap": True},
                            {"type": "TextBlock", "text": body, "wrap": True},
                        ],
                    },
                }],
            }
        elif ALERT_WEBHOOK_TYPE == "teams":
            payload = {
                "@type": "MessageCard",
                "@context": "http://schema.org/extensions",
                "themeColor": "D70000",
                "summary": subject,
                "title": subject,
                "text": body.replace("\n", "  \n"),  # MessageCard markdown line breaks
            }
        elif ALERT_WEBHOOK_TYPE == "slack":
            payload = {"text": f"*{subject}*\n{body}"}
        elif ALERT_WEBHOOK_TYPE == "discord":
            payload = {"content": text[:1900]}  # Discord 2000-char limit
        else:  # generic
            payload = {"text": text}
        try:
            req = urllib.request.Request(
                ALERT_WEBHOOK_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                ok = 200 <= resp.status < 300
            if ok:
                logger.info("[guardrail] alert posted to %s webhook: %s",
                            ALERT_WEBHOOK_TYPE, subject)
            else:
                logger.error("[guardrail] webhook returned non-2xx for: %s", subject)
            return ok
        except Exception as e:
            logger.error("[guardrail] failed to post alert webhook: %s", e)
            return False

    def _send_email(self, subject: str, body: str) -> bool:
        if not (SMTP_HOST and ALERT_EMAIL_TO and ALERT_EMAIL_FROM):
            return False
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = ALERT_EMAIL_FROM
            msg["To"] = ALERT_EMAIL_TO
            msg.set_content(body)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                if SMTP_USE_TLS:
                    s.starttls(context=ssl.create_default_context())
                if SMTP_USERNAME:
                    s.login(SMTP_USERNAME, SMTP_PASSWORD)
                s.send_message(msg)
            logger.info("[guardrail] alert email sent to %s: %s", ALERT_EMAIL_TO, subject)
            return True
        except Exception as e:
            logger.error("[guardrail] failed to send alert email: %s", e)
            return False


_monitor: Optional[GuardrailMonitor] = None


def get_monitor() -> GuardrailMonitor:
    global _monitor
    if _monitor is None:
        _monitor = GuardrailMonitor()
    return _monitor

"""Provision approved demo requests into the app_users invite allowlist.

Flow (the approval itself lives in Microsoft Approvals / Power Automate):

    demo request  ──▶  Approval  ──approved──▶  landing D1 row status='approved'
                                                        │  (Power Automate write)
    this worker ───poll──────────────────────────────▶─┘
      for each approved-not-provisioned row:
        if app_users already has this email:
          1. send the ACCOUNT-EXISTS email (no DB insert)
          2. POST /api/demo/provisioned  → row leaves the queue
        else (new user):
          1. insert app_users(email, is_active=true)   ← creates the account
          2. send the onboarding email (EMAIL_WORKFLOW_URL)
          3. POST /api/demo/provisioned  → row leaves the queue

Why a poller (not an inbound endpoint): the app DB lives on a private VM the
Microsoft cloud can't reach, and we don't want to expose an account-creating
endpoint. So the VM PULLS its work from the landing edge over authenticated
HTTPS and does the provisioning locally.

Idempotency: the app_users upsert is idempotent, and a row is only removed from
the queue (step 3) after the email send succeeds. A crash between steps 2 and 3
can re-send one onboarding email on the next cycle — tolerated and logged; it
never creates a duplicate account or a silently-missing invite.

Config (env):
    LANDING_API_BASE        landing origin, default https://taperead.trade
    PROVISION_TOKEN         bearer for /api/demo/queue + /provisioned  (required)
    EMAIL_WORKFLOW_URL      Power Automate flow that sends mail          (required)
    ONBOARDING_SIGNIN_URL   app sign-in URL, default https://terminal.taperead.trade
    PROVISION_POLL_INTERVAL_S  loop cadence in seconds (default 60), --loop only
    DATABASE_URL            same as the API (used to write app_users)

Run:
    python -m bharat_terminal.onboarding.provision_worker          # drain once, exit
    python -m bharat_terminal.onboarding.provision_worker --loop   # keep polling
    python -m bharat_terminal.onboarding.provision_worker --dry-run # no writes/email
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

import httpx
from sqlalchemy import text

from bharat_terminal.api.db import close_engine, get_session_factory

logger = logging.getLogger("provision_worker")

LANDING_API_BASE = os.getenv("LANDING_API_BASE", "https://taperead.trade").rstrip("/")
PROVISION_TOKEN = os.getenv("PROVISION_TOKEN", "")
EMAIL_WORKFLOW_URL = os.getenv("EMAIL_WORKFLOW_URL", "")
ONBOARDING_SIGNIN_URL = os.getenv("ONBOARDING_SIGNIN_URL", "https://terminal.taperead.trade")
POLL_INTERVAL_S = float(os.getenv("PROVISION_POLL_INTERVAL_S", "60"))

HTTP_TIMEOUT = httpx.Timeout(15.0)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {PROVISION_TOKEN}"}


async def fetch_queue(client: httpx.AsyncClient) -> list[dict]:
    """Approved-not-yet-provisioned requests from the landing edge."""
    resp = await client.get(
        f"{LANDING_API_BASE}/api/demo/queue", headers=_auth_headers()
    )
    resp.raise_for_status()
    return resp.json().get("requests", [])


async def account_exists(email: str) -> bool:
    """True if an app_users row already exists for this email (any is_active)."""
    factory = get_session_factory()
    async with factory() as s:
        row = (await s.execute(
            text("SELECT 1 FROM app_users WHERE lower(email) = :e LIMIT 1"),
            {"e": email},
        )).first()
    return row is not None


async def create_account(email: str) -> None:
    """Add the invite-allowlist row. Only called for emails not already present
    (existence is checked first), so ON CONFLICT DO NOTHING is just a race guard —
    we never reactivate/overwrite an existing account here."""
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO app_users (email, is_active) VALUES (:e, true) "
                "ON CONFLICT (email) DO NOTHING"
            ),
            {"e": email},
        )
        await s.commit()


# Email presentation — wrap inner HTML in a well-formed, styled container +
# identity footer (small legitimacy signals for spam filters). Kept in sync with
# the landing helpers (functions/api/demo/_otp.js: wrapHtml / EMAIL_FOOTER_TEXT).
_FOOTER_TEXT = (
    "\n\n—\nTaperead · research intelligence for Indian markets\nhttps://taperead.trade"
)


def _wrap_html(inner: str) -> str:
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.5;color:#1a1a1a">'
        + inner
        + '<hr style="border:none;border-top:1px solid #e5e5e5;margin:20px 0">'
        '<p style="font-size:12px;color:#888;margin:0">Taperead — research intelligence for '
        'Indian markets · <a href="https://taperead.trade" style="color:#888">taperead.trade</a></p></div>'
    )


def _onboarding_email(email: str) -> dict:
    subject = "Your Taperead Terminal access is active"
    text_body = (
        f"Hi,\n\n"
        f"Your request for Taperead Terminal has been approved and your access is now active.\n\n"
        f"To sign in, go to {ONBOARDING_SIGNIN_URL} and choose \"Sign in with Google\". "
        f"You must use this email address ({email}) — access is tied to it.\n\n"
        f"See you inside.\n"
        f"— The Taperead team\n"
        f"hi@taperead.trade"
    )
    html_body = (
        f"<p>Hi,</p>"
        f"<p>Your request for <strong>Taperead Terminal</strong> has been approved and "
        f"your access is now active.</p>"
        f"<p>To sign in, go to <a href=\"{ONBOARDING_SIGNIN_URL}\">{ONBOARDING_SIGNIN_URL}</a> "
        f"and choose <strong>Sign in with Google</strong>. You must use this email address "
        f"(<strong>{email}</strong>) — access is tied to it.</p>"
        f"<p>See you inside.<br>— The Taperead team<br>"
        f"<a href=\"mailto:hi@taperead.trade\">hi@taperead.trade</a></p>"
    )
    return {"to": email, "subject": subject, "html": _wrap_html(html_body),
            "text": text_body + _FOOTER_TEXT}


def _already_exists_email(email: str) -> dict:
    subject = "Your Taperead Terminal account already exists"
    text_body = (
        f"Hi,\n\n"
        f"Thanks for your interest in Taperead Terminal. An account for {email} "
        f"already exists, so there's nothing new to set up.\n\n"
        f"Just go to {ONBOARDING_SIGNIN_URL} and choose \"Sign in with Google\" "
        f"using this email address ({email}). If you're having trouble signing in, "
        f"reply to this email and we'll help.\n\n"
        f"— The Taperead team\n"
        f"hi@taperead.trade"
    )
    html_body = (
        f"<p>Hi,</p>"
        f"<p>Thanks for your interest in <strong>Taperead Terminal</strong>. An account for "
        f"<strong>{email}</strong> already exists, so there's nothing new to set up.</p>"
        f"<p>Just go to <a href=\"{ONBOARDING_SIGNIN_URL}\">{ONBOARDING_SIGNIN_URL}</a> and "
        f"choose <strong>Sign in with Google</strong> using this email address. If you're "
        f"having trouble signing in, reply to this email and we'll help.</p>"
        f"<p>— The Taperead team<br><a href=\"mailto:hi@taperead.trade\">hi@taperead.trade</a></p>"
    )
    return {"to": email, "subject": subject, "html": _wrap_html(html_body),
            "text": text_body + _FOOTER_TEXT}


async def send_onboarding_email(client: httpx.AsyncClient, email: str) -> None:
    resp = await client.post(EMAIL_WORKFLOW_URL, json=_onboarding_email(email))
    resp.raise_for_status()


async def send_already_exists_email(client: httpx.AsyncClient, email: str) -> None:
    resp = await client.post(EMAIL_WORKFLOW_URL, json=_already_exists_email(email))
    resp.raise_for_status()


async def mark_provisioned(client: httpx.AsyncClient, req_id: int) -> None:
    resp = await client.post(
        f"{LANDING_API_BASE}/api/demo/provisioned",
        headers=_auth_headers(),
        json={"id": req_id},
    )
    resp.raise_for_status()


async def drain_once(dry_run: bool = False) -> int:
    """Process every currently-approved request. Returns the count provisioned."""
    if not PROVISION_TOKEN:
        raise RuntimeError("PROVISION_TOKEN is not set — cannot pull the queue.")
    if not EMAIL_WORKFLOW_URL and not dry_run:
        raise RuntimeError("EMAIL_WORKFLOW_URL is not set — cannot send onboarding email.")

    provisioned = 0
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        queue = await fetch_queue(client)
        if not queue:
            logger.info("queue empty — nothing to provision")
            return 0
        logger.info("pulled %d approved request(s)", len(queue))

        for req in queue:
            req_id = req.get("id")
            email = (req.get("email") or "").strip().lower()
            if not email or req_id is None:
                logger.error("skipping malformed queue row: %r", req)
                continue
            try:
                exists = await account_exists(email)
                if dry_run:
                    logger.info(
                        "[dry-run] id=%s email=%s → would %s",
                        req_id, email,
                        "send ACCOUNT-EXISTS email (no insert)" if exists
                        else "create account + send onboarding email",
                    )
                    continue
                if exists:
                    # Already provisioned before: do NOT re-insert; send the
                    # account-exists email instead of onboarding, then mark done.
                    await send_already_exists_email(client, email)
                    await mark_provisioned(client, req_id)
                    provisioned += 1
                    logger.info("already-exists id=%s email=%s (no insert)", req_id, email)
                else:
                    # New user: 1) insert → 2) onboarding email (must succeed) → 3) mark done.
                    await create_account(email)
                    await send_onboarding_email(client, email)
                    await mark_provisioned(client, req_id)
                    provisioned += 1
                    logger.info("provisioned id=%s email=%s", req_id, email)
            except Exception as e:  # one bad row must not stall the rest
                logger.error(
                    "failed to provision id=%s email=%s: %s — will retry next cycle",
                    req_id, email, str(e),
                )
    return provisioned


async def run_loop(dry_run: bool = False) -> None:
    from bharat_terminal.heartbeat import abeat
    logger.info("provision worker loop starting (interval=%.0fs)", POLL_INTERVAL_S)
    while True:
        # Liveness beat for /admin/health; TTL scaled to the poll interval.
        await abeat("onboarding", ttl=max(180, int(POLL_INTERVAL_S) * 2 + 60))
        try:
            await drain_once(dry_run=dry_run)
        except Exception as e:
            logger.error("drain cycle failed: %s", str(e))
        await asyncio.sleep(POLL_INTERVAL_S)


async def _main(args) -> int:
    try:
        if args.loop:
            await run_loop(dry_run=args.dry_run)
            return 0
        n = await drain_once(dry_run=args.dry_run)
        logger.info("done — provisioned %d request(s)", n)
        return 0
    finally:
        await close_engine()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="Provision approved demo requests.")
    ap.add_argument("--loop", action="store_true", help="poll continuously")
    ap.add_argument("--dry-run", action="store_true", help="no writes, no email — log only")
    args = ap.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())

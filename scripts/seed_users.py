"""
Seed invited alpha users into the app_users allowlist.

Access is invite-only: a Google sign-in succeeds only if the verified email
matches an active row in app_users. This script inserts (or re-activates) the
given emails.

Usage:
    python -m scripts.seed_users alice@firm.com bob@fund.in
    python -m scripts.seed_users --file invites.txt        # one email per line
    python -m scripts.seed_users --list                    # show current allowlist
    python -m scripts.seed_users --deactivate carol@x.com  # revoke access

Requires DATABASE_URL in the environment (same as the API).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text

from bharat_terminal.api.db import close_engine, get_session_factory


def _norm(email: str) -> str:
    return email.strip().lower()


async def _upsert(emails: list[str]) -> None:
    factory = get_session_factory()
    async with factory() as s:
        for email in emails:
            await s.execute(
                text(
                    "INSERT INTO app_users (email, is_active) VALUES (:e, true) "
                    "ON CONFLICT (email) DO UPDATE SET is_active = true"
                ),
                {"e": email},
            )
        await s.commit()
    print(f"Seeded/activated {len(emails)} user(s): {', '.join(emails)}")


async def _deactivate(emails: list[str]) -> None:
    factory = get_session_factory()
    async with factory() as s:
        for email in emails:
            await s.execute(
                text("UPDATE app_users SET is_active = false WHERE lower(email) = :e"),
                {"e": email},
            )
        await s.commit()
    print(f"Deactivated {len(emails)} user(s): {', '.join(emails)}")


async def _list() -> None:
    factory = get_session_factory()
    async with factory() as s:
        rows = (await s.execute(
            text(
                "SELECT email, is_active, invited_at, last_login_at "
                "FROM app_users ORDER BY invited_at"
            )
        )).mappings().all()
    if not rows:
        print("app_users is empty — no one is invited yet.")
        return
    print(f"{'EMAIL':<40} {'ACTIVE':<7} LAST LOGIN")
    for r in rows:
        print(f"{r['email']:<40} {str(r['is_active']):<7} {r['last_login_at'] or '-'}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Manage the app_users invite allowlist.")
    ap.add_argument("emails", nargs="*", help="emails to invite")
    ap.add_argument("--file", help="file with one email per line")
    ap.add_argument("--deactivate", action="store_true", help="deactivate the given emails")
    ap.add_argument("--list", action="store_true", help="print the current allowlist")
    args = ap.parse_args()

    try:
        if args.list:
            await _list()
            return 0

        emails = [_norm(e) for e in args.emails]
        if args.file:
            with open(args.file) as f:
                emails += [_norm(line) for line in f if line.strip() and not line.startswith("#")]
        emails = [e for e in dict.fromkeys(emails) if e]  # dedupe, keep order

        if not emails:
            ap.print_help()
            return 1

        if args.deactivate:
            await _deactivate(emails)
        else:
            await _upsert(emails)
        return 0
    finally:
        await close_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

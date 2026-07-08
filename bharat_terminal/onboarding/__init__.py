"""Onboarding: turn approved demo requests into invited app_users.

See ``provision_worker`` — the VM-side poller that pulls approved demo requests
from the landing-page edge (Cloudflare D1), creates the invite-allowlist row,
and sends the onboarding email.
"""

"""Adapter liveness — stall detection + self-heal respawn gating (pure).

Extracted from ingestion/main.py on 2026-08-16, after the BSE_FILINGS
adapter parked silently for 20+ minutes following the EC2 hard-crash
recovery (one healthy cycle, then a wedged await with no exception and no
log — the 2026-07-20 disease, new host). The liveness watchdog previously
ALERTED on a stalled loop but could not heal it; a human restart was the
only cure. main.py's watchdog now cancels and respawns a parked adapter
task, gated by the pure functions here so the policy is unit-testable
without the service's heavy imports (aiokafka/aiohttp/redis).

Threshold model: an adapter is STALLED once idle exceeds
max(3 x its slow poll interval, stall_after) — three missed slow cycles is
never normal, and the floor keeps fast-polling adapters from tripping on a
single long cycle. RESPAWN uses the same shape with its own (higher) floor,
plus a per-adapter minimum gap so a genuinely broken adapter becomes a
respawn-every-N-minutes heartbeat instead of a flap loop.
"""
from __future__ import annotations


def stall_threshold(adapter, floor_s: float) -> float:
    """Idle seconds beyond which this adapter's loop counts as parked."""
    slow = (getattr(adapter, "poll_interval_slow", None)
            or getattr(adapter, "poll_interval_seconds", None) or 60.0)
    return max(3.0 * float(slow), float(floor_s))


def stalled_adapters(adapters, now: float, stall_after: float):
    """[(source_name, idle_s)] for adapters whose loop froze. Adapters that
    never completed a first cycle (last_cycle_ts unset) are skipped — boot
    slowness is the circuit breaker's problem, not the watchdog's."""
    out = []
    for a in adapters:
        ts = getattr(a, "last_cycle_ts", None)
        if ts is None:
            continue
        idle = now - ts
        if idle > stall_threshold(a, stall_after):
            out.append((a.source_name, idle))
    return out


def respawn_candidates(adapters, now: float, respawn_after: float,
                       last_respawn: dict, min_gap_s: float):
    """[(adapter, idle_s)] whose task should be cancelled and respawned.

    Fires only past the respawn floor (strictly above the alert floor —
    alert first, heal second) and never more often than min_gap_s per
    adapter. Callers must record last_respawn[source_name] = now when they
    act, or the gate cannot hold.
    """
    out = []
    for a in adapters:
        ts = getattr(a, "last_cycle_ts", None)
        if ts is None:
            continue
        idle = now - ts
        if idle <= stall_threshold(a, respawn_after):
            continue
        prev = last_respawn.get(a.source_name)
        if prev is not None and (now - prev) < min_gap_s:
            continue
        out.append((a, idle))
    return out

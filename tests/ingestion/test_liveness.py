"""ingestion.liveness — stall detection + self-heal respawn gating.

Born from the 2026-08-16 BSE_FILINGS silent wedge: one healthy cycle after
the EC2 hard-crash recovery, then a parked await for 20+ minutes with no
exception and no log. The watchdog alerted but could not heal; these tests
pin the pure policy that now drives cancel-and-respawn in ingestion/main.py.

The module is deliberately stdlib-only so this file needs none of the
service's heavy imports (aiokafka/aiohttp/redis).
"""
from types import SimpleNamespace

from bharat_terminal.ingestion.liveness import (respawn_candidates,
                                                stall_threshold,
                                                stalled_adapters)


def adapter(name="BSE_FILINGS", slow=300.0, base=120.0, last=None):
    return SimpleNamespace(source_name=name, poll_interval_slow=slow,
                           poll_interval_seconds=base, last_cycle_ts=last)


# ── stall_threshold ──────────────────────────────────────────────────────

def test_threshold_is_three_slow_cycles():
    # BSE: slow=300 → three missed slow cycles = 900s (the observed alert
    # fired at 918s — this is the exact production threshold).
    assert stall_threshold(adapter(slow=300.0), 300.0) == 900.0


def test_threshold_floor_dominates_fast_pollers():
    # A 15s-slow adapter must not trip on one long cycle: the floor holds.
    assert stall_threshold(adapter(slow=15.0), 300.0) == 300.0


def test_threshold_falls_back_to_legacy_interval():
    a = adapter(slow=None, base=120.0)
    assert stall_threshold(a, 300.0) == 360.0


# ── stalled_adapters ─────────────────────────────────────────────────────

def test_healthy_adapter_not_stalled():
    a = adapter(last=1000.0)
    assert stalled_adapters([a], now=1000.0 + 899.0, stall_after=300.0) == []


def test_stalled_past_threshold():
    a = adapter(last=1000.0)
    out = stalled_adapters([a], now=1000.0 + 901.0, stall_after=300.0)
    assert out == [("BSE_FILINGS", 901.0)]


def test_never_started_adapter_is_skipped():
    # Boot slowness is not a stall — last_cycle_ts unset means the run loop
    # hasn't reached its first iteration yet.
    a = adapter(last=None)
    assert stalled_adapters([a], now=1e9, stall_after=300.0) == []


# ── respawn_candidates ───────────────────────────────────────────────────

def test_respawn_fires_past_its_own_floor():
    # respawn_after=1200 > 3*slow=900 → the higher floor governs: alert
    # territory (901-1200s) does NOT respawn; past 1200s it does.
    a = adapter(last=1000.0)
    assert respawn_candidates([a], now=1000.0 + 1100.0, respawn_after=1200.0,
                              last_respawn={}, min_gap_s=600.0) == []
    out = respawn_candidates([a], now=1000.0 + 1201.0, respawn_after=1200.0,
                             last_respawn={}, min_gap_s=600.0)
    assert out and out[0][0] is a and out[0][1] == 1201.0


def test_respawn_rate_limited_per_adapter():
    a = adapter(last=1000.0)
    now = 1000.0 + 1300.0
    # A respawn 5 minutes ago blocks; one 11 minutes ago allows.
    assert respawn_candidates([a], now, 1200.0,
                              {"BSE_FILINGS": now - 300.0}, 600.0) == []
    assert respawn_candidates([a], now, 1200.0,
                              {"BSE_FILINGS": now - 660.0}, 600.0) != []


def test_respawn_gate_is_per_adapter():
    stuck = adapter(name="BSE_FILINGS", last=1000.0)
    healthy = adapter(name="NSE_FILINGS", last=2290.0)
    now = 1000.0 + 1300.0
    out = respawn_candidates([stuck, healthy], now, 1200.0,
                             {"NSE_FILINGS": now - 10.0}, 600.0)
    # Only the stuck one respawns; NSE is healthy AND recently respawned.
    assert [c[0].source_name for c in out] == ["BSE_FILINGS"]


def test_never_started_adapter_never_respawns():
    a = adapter(last=None)
    assert respawn_candidates([a], now=1e9, respawn_after=1200.0,
                              last_respawn={}, min_gap_s=600.0) == []

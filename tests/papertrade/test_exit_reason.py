"""PaperEngine._exit_reason — the most consequential pure function in the
repo: squareoff, vol-scaled trailing stops, flat-stop fallback, per-strategy
holds, pead/hedge exemptions. Pure static method of (row, ltp, now_ist).

Review-finding regressions carried here:
  C2 (CRITICAL): holds count calendar days; validated PEAD horizon is 20
      TRADING days -> xfail(strict=True) until fixed.
  Comment-vs-code discrepancy (flagged 2026-07-31): engine.py says
      "orderwin_long, like pead_long, is an unconditional hold (no stop)"
      but the code exempts only pead_long. Current behavior is PINNED below
      (test_orderwin_currently_gets_trailing_stop) so whichever way the
      founder decides, the change is deliberate and visible.
"""
import pytest

from bharat_terminal.papertrade import engine as eng
from conftest import ist_dt, open_row

exit_reason = eng.PaperEngine._exit_reason


# ── 1. cash-short squareoff ──────────────────────────────────────────────

def test_cash_short_squareoff_at_1515_exactly():
    r = open_row(side="SHORT", instrument="CASH",
                 entry_at=ist_dt(2026, 7, 30, 10, 0))
    assert exit_reason(r, 100.0, ist_dt(2026, 7, 30, 15, 15, 0)) == "squareoff"


def test_cash_short_not_squared_off_one_second_before():
    r = open_row(side="SHORT", instrument="CASH", entry_price=100.0,
                 entry_at=ist_dt(2026, 7, 30, 10, 0))
    # ltp == entry: no stop; held 0 days: no time exit
    assert exit_reason(r, 100.0, ist_dt(2026, 7, 30, 15, 14, 59)) is None


def test_cash_short_overnight_survivor_flattened_next_morning():
    # Service down at close -> flatten at next opportunity, even at 09:20.
    r = open_row(side="SHORT", instrument="CASH",
                 entry_at=ist_dt(2026, 7, 29, 14, 0))
    assert exit_reason(r, 100.0, ist_dt(2026, 7, 30, 9, 20)) == "squareoff"


def test_fut_short_exempt_from_squareoff():
    r = open_row(side="SHORT", instrument="FUT", entry_price=100.0,
                 entry_at=ist_dt(2026, 7, 30, 10, 0))
    assert exit_reason(r, 100.0, ist_dt(2026, 7, 30, 15, 20)) is None


def test_long_cash_exempt_from_squareoff():
    r = open_row(strategy="pead_long", side="LONG", instrument="CASH",
                 entry_price=100.0, entry_at=ist_dt(2026, 7, 30, 10, 0))
    assert exit_reason(r, 100.0, ist_dt(2026, 7, 30, 15, 20)) is None


# ── 2. pead_hedge is fully exempt ────────────────────────────────────────

def test_pead_hedge_never_exits():
    # No stop, no hold, no squareoff (LONG leg) — hedge_loop owns its lifecycle.
    r = open_row(strategy="pead_hedge", side="LONG", instrument="CASH",
                 entry_price=100.0, entry_vol60=0.01,
                 entry_at=ist_dt(2026, 6, 1, 10, 0))
    # 50% collapse, 59 days held: still None
    assert exit_reason(r, 50.0, ist_dt(2026, 7, 30, 15, 25)) is None


# ── 3. vol-scaled trailing stop ──────────────────────────────────────────

def test_trailing_stop_short_triggers_at_exact_threshold():
    # dist = PAPER_TRAIL_SIGMA * vol; anchor = trail_extreme
    vol = 0.01
    anchor = 90.0
    trigger = anchor * (1 + eng.PAPER_TRAIL_SIGMA * vol)   # 91.8 at sigma=2
    r = open_row(side="SHORT", instrument="FUT", entry_price=100.0,
                 trail_extreme=anchor, entry_vol60=vol,
                 entry_at=ist_dt(2026, 7, 30, 10, 0))
    now = ist_dt(2026, 7, 30, 11, 0)
    assert exit_reason(r, trigger, now) == "trail_stop"          # >= inclusive
    assert exit_reason(r, trigger - 0.01, now) is None


def test_trailing_stop_long_triggers_at_exact_threshold():
    vol = 0.01
    anchor = 110.0
    trigger = anchor * (1 - eng.PAPER_TRAIL_SIGMA * vol)   # 107.8 at sigma=2
    r = open_row(strategy="orderwin_long", side="LONG", instrument="CASH",
                 entry_price=100.0, trail_extreme=anchor, entry_vol60=vol,
                 entry_at=ist_dt(2026, 7, 30, 10, 0))
    now = ist_dt(2026, 7, 30, 11, 0)
    assert exit_reason(r, trigger, now) == "trail_stop"
    assert exit_reason(r, trigger + 0.01, now) is None


def test_trail_anchor_falls_back_to_entry_when_extreme_null():
    vol = 0.01
    r = open_row(side="SHORT", instrument="FUT", entry_price=100.0,
                 trail_extreme=None, entry_vol60=vol,
                 entry_at=ist_dt(2026, 7, 30, 10, 0))
    trigger = 100.0 * (1 + eng.PAPER_TRAIL_SIGMA * vol)
    assert exit_reason(r, trigger, ist_dt(2026, 7, 30, 11, 0)) == "trail_stop"


def test_flat_stop_fallback_when_vol_unavailable():
    # entry_vol60 None -> flat PAPER_STOP_PCT stop off ENTRY (not anchor)
    stop = eng.PAPER_STOP_PCT / 100.0
    r = open_row(side="SHORT", instrument="FUT", entry_price=100.0,
                 trail_extreme=95.0, entry_vol60=None,
                 entry_at=ist_dt(2026, 7, 30, 10, 0))
    now = ist_dt(2026, 7, 30, 11, 0)
    assert exit_reason(r, 100.0 * (1 + stop), now) == "stop"
    assert exit_reason(r, 100.0 * (1 + stop) - 0.01, now) is None


def test_flat_stop_fallback_when_vol_zero():
    stop = eng.PAPER_STOP_PCT / 100.0
    r = open_row(strategy="tailwind_long", side="LONG", instrument="CASH",
                 entry_price=100.0, entry_vol60=0.0,
                 entry_at=ist_dt(2026, 7, 30, 10, 0))
    assert exit_reason(r, 100.0 * (1 - stop),
                       ist_dt(2026, 7, 30, 11, 0)) == "stop"


def test_no_stop_evaluation_without_ltp():
    # ltp None -> stop block skipped entirely; only time-based exits apply
    r = open_row(side="SHORT", instrument="FUT", entry_price=100.0,
                 entry_vol60=0.01, entry_at=ist_dt(2026, 7, 30, 10, 0))
    assert exit_reason(r, None, ist_dt(2026, 7, 30, 11, 0)) is None


def test_pead_long_exempt_from_all_stops():
    # 50% collapse with vol available: pead_long must NOT stop out —
    # its validated edge is an unconditional hold.
    r = open_row(strategy="pead_long", side="LONG", instrument="CASH",
                 entry_price=100.0, entry_vol60=0.01, trail_extreme=120.0,
                 entry_at=ist_dt(2026, 7, 30, 10, 0))
    assert exit_reason(r, 50.0, ist_dt(2026, 7, 30, 11, 0)) is None


def test_orderwin_currently_gets_trailing_stop():
    """PINS CURRENT BEHAVIOR — engine.py:563 comment claims orderwin_long is
    'an unconditional hold (no stop)' but the code exempts only pead_long.
    Discrepancy flagged in the 2026-07-30 review; if the code is changed to
    match the comment, update this test alongside — deliberately."""
    vol = 0.01
    r = open_row(strategy="orderwin_long", side="LONG", instrument="CASH",
                 entry_price=100.0, entry_vol60=vol,
                 entry_at=ist_dt(2026, 7, 30, 10, 0))
    trigger = 100.0 * (1 - eng.PAPER_TRAIL_SIGMA * vol)
    assert exit_reason(r, trigger, ist_dt(2026, 7, 30, 11, 0)) == "trail_stop"


# ── 4. holding periods ───────────────────────────────────────────────────

def test_pead_time_exit_at_hold_boundary():
    r = open_row(strategy="pead_long", side="LONG", instrument="CASH",
                 entry_price=100.0, entry_at=ist_dt(2026, 7, 1, 10, 0))
    hold = eng.PAPER_PEAD_HOLD_DAYS
    day_before = ist_dt(2026, 7, 1 + hold - 1, 11, 0)
    on_day = ist_dt(2026, 7, 1 + hold, 11, 0)
    assert exit_reason(r, 100.0, day_before) is None
    assert exit_reason(r, 100.0, on_day) == "time_exit"


def test_fut_short_time_exit_after_short_hold():
    r = open_row(side="SHORT", instrument="FUT", entry_price=100.0,
                 entry_at=ist_dt(2026, 7, 20, 10, 0))
    hold = eng.PAPER_SHORT_HOLD_DAYS
    assert exit_reason(r, 100.0, ist_dt(2026, 7, 20 + hold - 1, 11, 0)) is None
    assert exit_reason(r, 100.0,
                       ist_dt(2026, 7, 20 + hold, 11, 0)) == "time_exit"


def test_time_exit_fires_even_without_ltp():
    # run_exits keeps the row open until a quote exists, but the REASON is due.
    r = open_row(strategy="pead_long", side="LONG", instrument="CASH",
                 entry_at=ist_dt(2026, 6, 1, 10, 0))
    assert exit_reason(r, None, ist_dt(2026, 7, 30, 11, 0)) == "time_exit"


@pytest.mark.xfail(
    strict=True,
    reason="C2 (CRITICAL, review 2026-07-30): holds count CALENDAR days but "
           "the validated PEAD horizon is 20 TRADING days — 2026-07-03 to "
           "2026-07-23 is 20 calendar but only 14 trading days, so the "
           "position must still be held. Remove this marker when the hold "
           "logic switches to trading days.")
def test_c2_pead_hold_counts_trading_days():
    r = open_row(strategy="pead_long", side="LONG", instrument="CASH",
                 entry_price=100.0, entry_at=ist_dt(2026, 7, 3, 11, 0))
    # 14 trading days elapsed (Jul 6-10, 13-17, 20-23) — under a 20
    # TRADING-day hold this must not exit yet.
    assert exit_reason(r, 100.0, ist_dt(2026, 7, 23, 11, 0)) is None

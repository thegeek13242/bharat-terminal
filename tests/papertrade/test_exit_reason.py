"""PaperEngine._exit_reason — the most consequential pure function in the
repo: squareoff, vol-scaled trailing stops, flat-stop fallback, per-strategy
holds, pead/hedge exemptions. Pure static method of (row, ltp, now_ist).

Review-finding regressions carried here:
  C2 (CRITICAL, review 2026-07-30) FIXED 2026-08-15: holds now count
      TRADING days via market_hours.trading_days_between — weekends and
      NSE holidays no longer advance the clock.
  Comment-vs-code discrepancy (flagged 2026-07-31): the stale engine.py
      comment claiming orderwin_long is "an unconditional hold (no stop)"
      was removed 2026-08-15; the CODE still applies the trailing stop to
      orderwin_long, and that behavior remains PINNED below
      (test_orderwin_currently_gets_trailing_stop). Removing the stop is a
      founder decision, not a drive-by.
"""
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


# ── 4. holding periods (TRADING days — C2 fixed 2026-08-15) ──────────────

def test_pead_time_exit_at_hold_boundary():
    # Entry Wed 2026-07-01; 20 TRADING days later is Wed 2026-07-29
    # (Jul 2-3, 6-10, 13-17, 20-24, 27-29 — no July holidays in 2026).
    r = open_row(strategy="pead_long", side="LONG", instrument="CASH",
                 entry_price=100.0, entry_at=ist_dt(2026, 7, 1, 10, 0))
    assert eng.PAPER_PEAD_HOLD_DAYS == 20     # test.sh pins the env
    assert exit_reason(r, 100.0, ist_dt(2026, 7, 28, 11, 0)) is None
    assert exit_reason(r, 100.0, ist_dt(2026, 7, 29, 11, 0)) == "time_exit"


def test_fut_short_time_exit_after_short_hold():
    # Entry Mon 2026-07-20; 5 TRADING days later is Mon 2026-07-27
    # (Jul 21-24 are 4 sessions; the weekend does not advance the clock).
    r = open_row(side="SHORT", instrument="FUT", entry_price=100.0,
                 entry_at=ist_dt(2026, 7, 20, 10, 0))
    assert eng.PAPER_SHORT_HOLD_DAYS == 5
    assert exit_reason(r, 100.0, ist_dt(2026, 7, 24, 11, 0)) is None
    assert exit_reason(r, 100.0, ist_dt(2026, 7, 27, 11, 0)) == "time_exit"


def test_hold_clock_skips_nse_holidays():
    # Entry Wed 2026-09-30, 5-session hold: Oct 1, 5, 6, 7, 8 — Gandhi
    # Jayanti (Fri Oct 2) plus the weekend add three calendar days.
    r = open_row(strategy="orderwin_long", side="LONG", instrument="CASH",
                 entry_price=100.0, entry_at=ist_dt(2026, 9, 30, 10, 0))
    assert exit_reason(r, 100.0, ist_dt(2026, 10, 7, 11, 0)) is None
    assert exit_reason(r, 100.0, ist_dt(2026, 10, 8, 11, 0)) == "time_exit"


def test_time_exit_fires_even_without_ltp():
    # run_exits keeps the row open until a quote exists, but the REASON is due.
    r = open_row(strategy="pead_long", side="LONG", instrument="CASH",
                 entry_at=ist_dt(2026, 6, 1, 10, 0))
    assert exit_reason(r, None, ist_dt(2026, 7, 30, 11, 0)) == "time_exit"


def test_c2_pead_hold_counts_trading_days():
    # C2 (review 2026-07-30), FIXED 2026-08-15: 2026-07-03 -> 2026-07-23 is
    # 20 calendar but only 14 trading days — the position must still be held.
    r = open_row(strategy="pead_long", side="LONG", instrument="CASH",
                 entry_price=100.0, entry_at=ist_dt(2026, 7, 3, 11, 0))
    assert exit_reason(r, 100.0, ist_dt(2026, 7, 23, 11, 0)) is None

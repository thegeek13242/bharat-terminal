"""PaperEngine.fill_pending — the queued-fill path for off-session signals.

2026-07-30 review findings, all FIXED 2026-08-15:
  C3 (CRITICAL): gross cap is now checked on the FINAL qty, after futures
      lot inflation (fill_pending mirrors open_position's gate order).
  M2 (MAJOR): lot logic now runs before the zero_qty check, so a
      high-priced FUT pending fills as one lot like the live path.
  C1 (CRITICAL): fill-price selection is extracted into main.py's pure
      select_fill_price() — no `open` (no print today) means NO fill, so
      non-auction series (BE/BZ/SM/ST, BSE) can no longer fill at
      yesterday's carried-over LTP.

Also covers expire_stale_pending (2026-08-15): PENDING rows older than
PAPER_PENDING_MAX_AGE_H become auditable 'pending_expired' rejects instead
of parking invisibly forever (the Aug 7-12 stuck-queue class).
"""
import json
import math
from datetime import datetime, time as dtime, timedelta, timezone

from bharat_terminal.papertrade import engine as eng
from bharat_terminal.papertrade.main import select_fill_price
from conftest import pending_row

SLIP = eng.PAPER_SLIPPAGE_BPS / 1e4
NOTIONAL = eng.PAPER_NOTIONAL_PER_TRADE
CAP = eng.PAPER_GROSS_CAP


async def test_fills_at_supplied_price_with_slippage(engine, pool):
    pool.pending = [pending_row(id=1, side="SHORT")]
    n = await engine.fill_pending({"TEST": 100.0})
    assert n == 1
    ((row_id, qty, entry, prov_json, _vol),) = pool.fills
    assert row_id == 1
    assert entry == 100.0 * (1 - SLIP)
    assert qty == math.floor(NOTIONAL / entry)
    prov = json.loads(prov_json)
    assert prov["filled_from_pending"] is True
    assert prov["fill_price_source"] == "live_ltp"


async def test_fill_price_source_provenance(engine, pool):
    # Morning cycles pass the day's auction OPEN — the source must be stamped
    # so pilot forensics can separate auction fills from LTP fills.
    pool.pending = [pending_row(id=1, side="LONG")]
    await engine.fill_pending({"TEST": 100.0},
                              price_sources={"TEST": "auction_open"})
    prov = json.loads(pool.fills[0][3])
    assert prov["fill_price_source"] == "auction_open"


async def test_no_price_stays_pending_and_retries(engine, pool):
    # A made-up fill price would corrupt the pilot: no quote -> untouched row.
    pool.pending = [pending_row(id=1)]
    n = await engine.fill_pending({})
    assert n == 0
    assert not pool.fills and not pool.pending_rejects
    assert len(pool.pending) == 1


async def test_zero_price_stays_pending(engine, pool):
    pool.pending = [pending_row(id=1)]
    assert await engine.fill_pending({"TEST": 0.0}) == 0
    assert len(pool.pending) == 1


async def test_cash_zero_qty_pending_reject(engine, pool):
    pool.pending = [pending_row(id=1, side="SHORT")]
    await engine.fill_pending({"TEST": NOTIONAL * 2})
    ((row_id, reason, _prov),) = pool.pending_rejects
    assert (row_id, reason) == (1, "zero_qty")


async def test_gap_guard_applies_to_pead_pending(engine, pool):
    pool.prev_close["TEST"] = 100.0
    pool.pending = [pending_row(id=1, strategy="pead_long", side="LONG")]
    await engine.fill_pending({"TEST": 103.0})                  # +3% gap
    ((_, reason, prov_json),) = pool.pending_rejects
    assert reason == "gap_guard"
    assert json.loads(prov_json)["gap_pct"] == 3.0


async def test_gross_cap_rejects_pending_cash_fill(engine, pool):
    pool.gross = CAP                                            # book is full
    pool.pending = [pending_row(id=1, side="SHORT")]
    await engine.fill_pending({"TEST": 100.0})
    ((_, reason, _prov),) = pool.pending_rejects
    assert reason == "gross_cap"


async def test_fut_pending_quantizes_to_lots(engine, pool, fake_lots):
    fake_lots["TEST"] = 50
    pool.pending = [pending_row(id=1, side="SHORT", instrument="FUT")]
    await engine.fill_pending({"TEST": 100.0})
    ((_, qty, entry, prov_json, _vol),) = pool.fills
    assert qty % 50 == 0
    assert json.loads(prov_json)["lot_size"] == 50


async def test_fut_pending_unknown_lot_rejects(engine, pool, fake_lots):
    pool.pending = [pending_row(id=1, side="SHORT", instrument="FUT")]
    await engine.fill_pending({"TEST": 100.0})
    ((_, reason, _prov),) = pool.pending_rejects
    assert reason == "no_lot_size"


async def test_c3_gross_cap_holds_after_lot_quantization(engine, pool,
                                                         fake_lots):
    # C3 (review 2026-07-30), FIXED: the cap check runs on the final
    # lot-quantized qty, so a min-1-lot contract can no longer blow
    # through the gross cap unchecked.
    fake_lots["TEST"] = 500
    pool.gross = 400_000.0
    pool.pending = [pending_row(id=1, side="SHORT", instrument="FUT",
                                strategy="headwind_short")]
    # cash-sized check: ~50 shares x ~1998 = ~99,900 -> passes under the cap;
    # lot inflation: 1 lot = 500 x 1998 = 999,000 -> book would be 1,399,000.
    await engine.fill_pending({"TEST": 2000.0})
    if pool.fills:
        (_, qty, entry, _prov, _vol) = pool.fills[0]
        assert pool.gross + qty * entry <= CAP, (
            f"cap breached: {pool.gross} + {qty * entry} > {CAP}")
    else:
        ((_, reason, _prov),) = pool.pending_rejects
        assert reason == "gross_cap"


async def test_m2_fut_pending_min_one_lot_like_open_position(engine, pool,
                                                             fake_lots):
    # M2 (review 2026-07-30), FIXED: lot logic precedes the zero_qty check,
    # matching open_position — a high-priced FUT pending fills as one lot.
    # Price > per-trade notional (cash sizing would floor to 0) but the
    # 1-lot contract stays under the gross cap — the C3 fix correctly
    # rejects cap-busting contracts on BOTH paths now, so M2's scenario
    # must be cap-compatible to isolate the min-one-lot behavior.
    fake_lots["TEST"] = 2
    pool.pending = [pending_row(id=1, side="SHORT", instrument="FUT",
                                strategy="headwind_short")]
    await engine.fill_pending({"TEST": NOTIONAL * 1.2})
    assert pool.fills, "expected a 1-lot fill, got a zero_qty reject"
    assert pool.fills[0][1] == 2


# ── C1: fill-price selection (pure, extracted from mtm_loop 2026-08-15) ──

def test_c1_no_fill_without_todays_print():
    # `open` absent/zero = the symbol has not printed today (non-auction
    # BE/BZ/SM/ST + BSE series before their first trade; any symbol on a
    # data gap): the carried-over LTP must NEVER become a fill price.
    assert select_fill_price(None, dtime(9, 16)) is None
    assert select_fill_price({"ltp": 100.0}, dtime(9, 16)) is None
    assert select_fill_price({"ltp": 100.0, "open": None},
                             dtime(10, 0)) is None
    assert select_fill_price({"ltp": 100.0, "open": 0.0},
                             dtime(14, 0)) is None


def test_c1_auction_open_wins_before_0945():
    sel = select_fill_price({"ltp": 101.0, "open": 100.5}, dtime(9, 20))
    assert sel == (100.5, "auction_open_angelone")


def test_c1_live_ltp_wins_from_0945():
    sel = select_fill_price({"ltp": 101.0, "open": 100.5}, dtime(9, 45))
    assert sel == (101.0, "live_ltp")


def test_c1_open_present_but_no_ltp_after_0945_waits():
    assert select_fill_price({"ltp": None, "open": 100.5},
                             dtime(10, 0)) is None


# ── expire_stale_pending ─────────────────────────────────────────────────

async def test_stale_pending_expires_auditably(engine, pool):
    old = (datetime.now(timezone.utc)
           - timedelta(hours=eng.PAPER_PENDING_MAX_AGE_H + 1))
    pool.pending = [pending_row(id=1, created_at=old)]
    assert await engine.expire_stale_pending() == 1
    ((row_id, reason, prov_json),) = pool.pending_rejects
    assert (row_id, reason) == (1, "pending_expired")
    assert json.loads(prov_json)["expired_after_h"] == \
        eng.PAPER_PENDING_MAX_AGE_H


async def test_fresh_pending_not_expired(engine, pool):
    pool.pending = [pending_row(id=1)]        # created now
    assert await engine.expire_stale_pending() == 0
    assert not pool.pending_rejects and len(pool.pending) == 1

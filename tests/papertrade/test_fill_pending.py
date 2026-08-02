"""PaperEngine.fill_pending — the queued-fill path for off-session signals.

Carries the two strict xfails from the 2026-07-30 review:
  C3 (CRITICAL): gross cap checked on the cash-sized qty BEFORE futures lot
      inflation — a min-1-lot contract can blow through the cap unchecked.
  M2 (MAJOR): zero_qty reject runs before lot logic, so a high-priced FUT
      pending is rejected where the during-session path would open 1 lot.

C1 (fills before the pre-open auction) lives in main.py's mtm_loop, which is
not unit-testable without extracting the cycle body — see
test_c1_placeholder below.
"""
import json
import math

import pytest

from bharat_terminal.papertrade import engine as eng
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


@pytest.mark.xfail(
    strict=True,
    reason="C3 (CRITICAL, review 2026-07-30): fill_pending checks the gross "
           "cap on the CASH-sized qty, then inflates FUT qty to a minimum of "
           "one lot with no re-check — the filled contract can exceed the "
           "cap by the full lot value. open_position has the correct order. "
           "Remove marker when the cap is re-checked after lot quantization.")
async def test_c3_gross_cap_holds_after_lot_quantization(engine, pool,
                                                         fake_lots):
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


@pytest.mark.xfail(
    strict=True,
    reason="M2 (MAJOR, review 2026-07-30): the zero_qty reject runs before "
           "the lot logic, so a FUT pending on a high-priced symbol is "
           "permanently rejected where open_position would fill one lot. "
           "Remove marker when fill_pending mirrors open_position's order.")
async def test_m2_fut_pending_min_one_lot_like_open_position(engine, pool,
                                                             fake_lots):
    fake_lots["TEST"] = 10
    pool.pending = [pending_row(id=1, side="SHORT", instrument="FUT",
                                strategy="headwind_short")]
    await engine.fill_pending({"TEST": NOTIONAL * 1.5})
    assert pool.fills, "expected a 1-lot fill, got a zero_qty reject"
    assert pool.fills[0][1] == 10


@pytest.mark.skip(
    reason="C1 (CRITICAL, review 2026-07-30): queued fills execute at "
           "09:00:02 IST before the pre-open auction because mtm_loop's "
           "fill-price selection (main.py:227-278) falls back to stale LTP "
           "when the day's open is absent. The cycle body must be extracted "
           "into a testable function (design doc §2.2) before this can be a "
           "real test: given now=09:00 and open=0/absent, fill_pending must "
           "not be called with a live_ltp price for queued rows.")
async def test_c1_no_fill_before_auction_open():
    raise NotImplementedError

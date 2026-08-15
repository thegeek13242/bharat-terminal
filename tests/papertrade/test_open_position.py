"""PaperEngine.open_position — the during-session entry path: slippage,
cash/futures sizing, lot quantization, gap guard, gross cap, reject ladder,
idempotency. All I/O through the injected FakePool; the Angel One lot lookup
is patched via the fake_lots fixture."""
import math

from bharat_terminal.papertrade import engine as eng

SLIP = eng.PAPER_SLIPPAGE_BPS / 1e4
NOTIONAL = eng.PAPER_NOTIONAL_PER_TRADE
CAP = eng.PAPER_GROSS_CAP


# ── no_ltp reject ladder rung ────────────────────────────────────────────

async def test_no_ltp_rejects_auditably(engine, pool, make_intent):
    rid = await engine.open_position(make_intent(), None)
    assert rid is not None
    (rej,) = pool.rejected
    assert rej["reason"] == "no_ltp"
    assert not pool.opened


async def test_zero_and_negative_ltp_reject(engine, pool, make_intent):
    await engine.open_position(make_intent(impact_id="a"), 0.0)
    await engine.open_position(make_intent(impact_id="b"), -5.0)
    assert [r["reason"] for r in pool.rejected] == ["no_ltp", "no_ltp"]


# ── slippage + cash sizing ───────────────────────────────────────────────

async def test_long_entry_takes_adverse_slippage_up(engine, pool, make_intent):
    await engine.open_position(
        make_intent(side="LONG", strategy="orderwin_long"), 100.0)
    (row,) = pool.opened
    assert row["entry"] == 100.0 * (1 + SLIP)
    assert row["qty"] == math.floor(NOTIONAL / row["entry"])


async def test_short_entry_takes_adverse_slippage_down(engine, pool,
                                                       make_intent):
    await engine.open_position(make_intent(side="SHORT"), 100.0)
    (row,) = pool.opened
    assert row["entry"] == 100.0 * (1 - SLIP)
    assert row["qty"] == math.floor(NOTIONAL / row["entry"])


async def test_cash_zero_qty_reject_for_mrf_class_price(engine, pool,
                                                        make_intent):
    # entry above the whole per-trade notional -> qty 0 -> auditable reject
    await engine.open_position(make_intent(), NOTIONAL * 2)
    (rej,) = pool.rejected
    assert rej["reason"] == "zero_qty"


# ── futures lot quantization ─────────────────────────────────────────────

async def test_fut_qty_is_lot_multiple(engine, pool, make_intent, fake_lots):
    fake_lots["TEST"] = 500
    await engine.open_position(
        make_intent(instrument="FUT", side="SHORT"), 100.0)
    (row,) = pool.opened
    entry = 100.0 * (1 - SLIP)
    want = NOTIONAL / entry                        # ~1001 shares
    n_lots = max(1, math.floor(want / 500))
    assert row["qty"] == n_lots * 500
    assert row["qty"] % 500 == 0
    assert '"lot_size": 500' in row["prov"]


async def test_fut_minimum_one_lot_even_above_notional(engine, pool,
                                                       make_intent, fake_lots):
    # want < 1 lot -> still 1 lot: real contract values exceed the per-trade
    # notional, exactly as they do for a real small book (documented intent).
    fake_lots["TEST"] = 500
    await engine.open_position(
        make_intent(instrument="FUT", side="SHORT"), 1000.0)
    (row,) = pool.opened
    assert row["qty"] == 500
    assert row["qty"] * row["entry"] > NOTIONAL    # oversized by design
    assert '"n_lots": 1' in row["prov"]


async def test_fut_unknown_lot_size_rejects(engine, pool, make_intent,
                                            fake_lots):
    # empty lot table: inventing a quantity would fabricate an untradeable fill
    await engine.open_position(
        make_intent(instrument="FUT", side="SHORT"), 100.0)
    (rej,) = pool.rejected
    assert rej["reason"] == "no_lot_size"


async def test_pead_hedge_skips_lot_logic(engine, pool, make_intent,
                                          fake_lots):
    # pead_hedge quantizes by lot VALUE in the hedge loop instead
    await engine.open_position(
        make_intent(strategy="pead_hedge", side="LONG", instrument="FUT"),
        100.0)
    (row,) = pool.opened
    assert row["qty"] == math.floor(NOTIONAL / (100.0 * (1 + SLIP)))


# ── gross-exposure cap ───────────────────────────────────────────────────

async def test_gross_cap_rejects_when_exceeded(engine, pool, make_intent):
    entry = 100.0 * (1 - SLIP)
    cost = math.floor(NOTIONAL / entry) * entry
    pool.gross = CAP - cost + 1.0
    await engine.open_position(make_intent(), 100.0)
    (rej,) = pool.rejected
    assert rej["reason"] == "gross_cap"
    assert '"gross_at_reject"' in rej["prov"]


async def test_gross_cap_is_strictly_greater_than(engine, pool, make_intent):
    entry = 100.0 * (1 - SLIP)
    cost = math.floor(NOTIONAL / entry) * entry
    pool.gross = CAP - cost - 1.0                  # lands just under the cap
    await engine.open_position(make_intent(), 100.0)
    assert pool.opened and not pool.rejected


# ── PEAD gap guard ───────────────────────────────────────────────────────

async def test_gap_guard_rejects_above_threshold(engine, pool, make_intent):
    pool.prev_close["TEST"] = 100.0
    ltp = 100.0 * (1 + (eng.PAPER_PEAD_MAX_GAP_PCT + 0.5) / 100)   # +2.5%
    await engine.open_position(
        make_intent(strategy="pead_long", side="LONG"), ltp)
    (rej,) = pool.rejected
    assert rej["reason"] == "gap_guard"
    assert '"gap_pct": 2.5' in rej["prov"]


async def test_gap_guard_boundary(engine, pool, make_intent):
    # The comparison is strict (>), but an exactly-2.00% gap is not
    # representable in binary floats: 102.0/100.0 - 1 computes to
    # 2.0000000000000018%, so a nominal at-threshold print REJECTS in
    # practice. Pinned: just-below proceeds, nominal-2% rejects.
    pool.prev_close["TEST"] = 100.0
    await engine.open_position(
        make_intent(strategy="pead_long", side="LONG", impact_id="below"),
        101.99)                                                    # +1.99%
    assert pool.opened and not pool.rejected
    await engine.open_position(
        make_intent(strategy="pead_long", side="LONG", impact_id="at"),
        102.0)                                                     # +"2.00%"
    (rej,) = pool.rejected
    assert rej["reason"] == "gap_guard"


async def test_gap_guard_fails_open_without_prev_close(engine, pool,
                                                       make_intent):
    # No price_points history -> proceed (fail-open, paper; documented)
    await engine.open_position(
        make_intent(strategy="pead_long", side="LONG"), 100.0)
    assert pool.opened and not pool.rejected


async def test_gap_guard_only_applies_to_pead(engine, pool, make_intent):
    pool.prev_close["TEST"] = 100.0
    await engine.open_position(make_intent(strategy="headwind_short"), 110.0)
    assert pool.opened and not pool.rejected


# ── unknown-symbol gate (2026-08-15) ─────────────────────────────────────

async def test_unknown_symbol_rejects_at_open(engine, pool, make_intent):
    # A symbol absent from companies (upstream extraction error — the JNPR
    # case) must become an auditable reject, not an unfillable ledger row.
    pool.known_symbols = {"RELIANCE"}
    await engine.open_position(make_intent(symbol="JNPR"), 100.0)
    (rej,) = pool.rejected
    assert rej["reason"] == "unknown_symbol"
    assert not pool.opened


async def test_unknown_symbol_rejects_at_queue(engine, pool, make_intent):
    pool.known_symbols = {"RELIANCE"}
    await engine.queue_position(make_intent(symbol="JNPR"))
    (rej,) = pool.rejected
    assert rej["reason"] == "unknown_symbol"
    assert not pool.queued


async def test_hedge_symbol_exempt_from_companies_check(engine, pool,
                                                        make_intent):
    # NIFTYBEES (an ETF) is not a company; the hedge must not be gated.
    pool.known_symbols = {"RELIANCE"}
    await engine.open_position(
        make_intent(strategy="pead_hedge", symbol="NIFTYBEES", side="SHORT",
                    instrument="FUT"), 100.0)
    assert pool.opened and not pool.rejected


# ── idempotency ──────────────────────────────────────────────────────────

async def test_replayed_signal_is_idempotent_noop(engine, pool, make_intent):
    intent = make_intent()
    first = await engine.open_position(intent, 100.0)
    second = await engine.open_position(intent, 100.0)
    assert first is not None and second is None
    assert len(pool.opened) == 1 and not pool.rejected

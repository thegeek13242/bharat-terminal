"""Position ledger + portfolio accounting for the paper-trading pilot.

WHY the DB row IS the position state (no in-memory ledger): crash-safety by
construction. Every loop re-reads OPEN rows from paper_trades, so a restart
loses nothing and reconciliation is just "read the table". Idempotency comes
from INSERT .. ON CONFLICT (impact_id, symbol, strategy) DO NOTHING — a
replayed Kafka message can never double-open a position, the exact
order-idempotency lesson a live OMS needs, rehearsed from day one.

Fills are pessimistic on purpose: entry price takes an adverse slippage
haircut (shorts fill lower, longs higher) so the pilot's P&L cannot flatter
itself. Exits: cash shorts are intraday-only (>=15:15 IST squareoff — cash
short delivery is not a thing), FUT shorts hold PAPER_SHORT_HOLD_DAYS with a
PAPER_STOP_PCT stop, pead_long holds PAPER_PEAD_HOLD_DAYS (validated horizon).
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, time as dtime

import asyncpg

from bharat_terminal.analysis.market_hours import IST
from bharat_terminal.papertrade.rules import SignalIntent

logger = logging.getLogger(__name__)

PAPER_SLIPPAGE_BPS = float(os.getenv("PAPER_SLIPPAGE_BPS", "10"))
PAPER_NOTIONAL_PER_TRADE = float(os.getenv("PAPER_NOTIONAL_PER_TRADE", "100000"))
PAPER_CAPITAL = float(os.getenv("PAPER_CAPITAL", "1000000"))
PAPER_STOP_PCT = float(os.getenv("PAPER_STOP_PCT", "5.0"))
PAPER_SHORT_HOLD_DAYS = int(os.getenv("PAPER_SHORT_HOLD_DAYS", "5"))
PAPER_PEAD_HOLD_DAYS = int(os.getenv("PAPER_PEAD_HOLD_DAYS", "20"))
PAPER_ORDERWIN_HOLD_DAYS = int(os.getenv("PAPER_ORDERWIN_HOLD_DAYS", "5"))
# Vol-scaled trailing stop (founder decision 2026-07-28): trail distance =
# PAPER_TRAIL_SIGMA x entry sigma60, anchored to the position's favorable
# extreme. Replaces the flat 5% stop — a 5% stop is 8 sigma on a staid
# largecap and 1 sigma on an SME; vol scaling makes "limited loss" mean the
# same thing everywhere. pead_long is EXEMPT: its validated edge is an
# unconditional 20d hold and a trailing stop would gut exactly that.
PAPER_TRAIL_SIGMA = float(os.getenv("PAPER_TRAIL_SIGMA", "2.0"))
# Gross-exposure cap (founder decision 2026-07-29): total OPEN notional may
# never exceed this; breaching entries become auditable 'gross_cap' rejects.
# Defaults to PAPER_CAPITAL — no implicit leverage in the pilot.
PAPER_GROSS_CAP = float(os.getenv("PAPER_GROSS_CAP", "0")) or float(
    os.getenv("PAPER_CAPITAL", "1000000"))

_GROSS_SQL = """
SELECT coalesce(sum(abs(qty) * coalesce(mtm_price, entry_price)), 0)::float8
FROM paper_trades WHERE status = 'OPEN'
"""

# PEAD gap guard (founder decision 2026-07-30, from the batch-1 forensics:
# entries that paid a +3-6.6% post-surprise pop at fill are the losers —
# the validated close-to-close drift INCLUDES the overnight gap, so paying
# the gap forfeits the edge). Fill is rejected when the fill price exceeds
# the previous close by more than this percentage.
PAPER_PEAD_MAX_GAP_PCT = float(os.getenv("PAPER_PEAD_MAX_GAP_PCT", "2.0"))

_PREV_CLOSE_SQL = """
SELECT close FROM price_points
WHERE symbol = $1 AND date < (now() AT TIME ZONE 'Asia/Kolkata')::date
ORDER BY date DESC LIMIT 1
"""

_VOL60_SQL = """
SELECT stddev_samp(ret) AS vol60 FROM (
    SELECT close / nullif(lag(close) OVER (ORDER BY date), 0) - 1 AS ret
    FROM price_points
    WHERE symbol = $1 AND date >= CURRENT_DATE - 120
    ORDER BY date DESC LIMIT 61
) t
"""

# Cash shorts must be flat before close; 15:15 IST leaves a 15-min buffer.
SQUAREOFF_IST = dtime(15, 15)

_OPEN_SQL = """
INSERT INTO paper_trades
    (strategy, impact_id, news_id, symbol, side, instrument, qty,
     entry_price, entry_at, status, provenance, trail_extreme, entry_vol60)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::float8, now(), 'OPEN', $9::jsonb,
        $8::float8, $10::float8)
ON CONFLICT (impact_id, symbol, strategy) DO NOTHING
RETURNING id
"""

_REJECT_SQL = """
INSERT INTO paper_trades
    (strategy, impact_id, news_id, symbol, side, instrument, qty,
     status, exit_reason, provenance)
VALUES ($1, $2, $3, $4, $5, $6, 0, 'REJECTED', $7, $8::jsonb)
ON CONFLICT (impact_id, symbol, strategy) DO NOTHING
RETURNING id
"""

# Off-session signal: queue with qty=0/no entry — filled at the first live
# LTP after the next open (filling at the pre-news stale LTP would fabricate
# an impossible price; most results filings land after 15:30 IST).
_QUEUE_SQL = """
INSERT INTO paper_trades
    (strategy, impact_id, news_id, symbol, side, instrument, qty,
     status, provenance)
VALUES ($1, $2, $3, $4, $5, $6, 0, 'PENDING', $7::jsonb)
ON CONFLICT (impact_id, symbol, strategy) DO NOTHING
RETURNING id
"""

_PENDING_ROWS_SQL = """
SELECT id, strategy, symbol, side, instrument, created_at
FROM paper_trades WHERE status = 'PENDING'
"""

_FILL_SQL = """
UPDATE paper_trades
SET qty = $2, entry_price = $3::float8, entry_at = now(), status = 'OPEN',
    provenance = provenance || $4::jsonb,
    trail_extreme = $3::float8, entry_vol60 = $5::float8
WHERE id = $1 AND status = 'PENDING'
"""

_PENDING_REJECT_SQL = """
UPDATE paper_trades
SET status = 'REJECTED', exit_reason = $2, provenance = provenance || $3::jsonb
WHERE id = $1 AND status = 'PENDING'
"""

_MARK_SQL = """
UPDATE paper_trades
SET mtm_price = $2::float8, mtm_at = now(),
    pnl = (CASE WHEN side = 'LONG' THEN ($2::float8 - entry_price)
                ELSE (entry_price - $2::float8) END) * qty,
    trail_extreme = CASE WHEN side = 'SHORT'
                         THEN LEAST(coalesce(trail_extreme, entry_price), $2::float8)
                         ELSE GREATEST(coalesce(trail_extreme, entry_price), $2::float8)
                    END
WHERE id = $1 AND status = 'OPEN'
"""

_CLOSE_SQL = """
UPDATE paper_trades
SET status = 'CLOSED', exit_price = $2::float8, exit_at = now(),
    exit_reason = $3, mtm_price = $2::float8, mtm_at = now(),
    pnl = (CASE WHEN side = 'LONG' THEN ($2::float8 - entry_price)
                ELSE (entry_price - $2::float8) END) * qty
WHERE id = $1 AND status = 'OPEN'
RETURNING pnl
"""

_OPEN_ROWS_SQL = """
SELECT id, strategy, symbol, side, instrument, qty, entry_price, entry_at,
       trail_extreme, entry_vol60
FROM paper_trades WHERE status = 'OPEN'
"""

# equity = capital + realized + unrealized, computed in one statement so the
# snapshot is internally consistent (no read-skew between the three sums).
_SNAPSHOT_SQL = """
INSERT INTO paper_equity_snapshots
    (equity, realized_pnl, unrealized_pnl, gross_exposure, n_open)
SELECT $1::float8 + r.realized + u.unrealized,
       r.realized, u.unrealized, u.gross, u.n_open
FROM (SELECT coalesce(sum(pnl), 0) AS realized
      FROM paper_trades WHERE status = 'CLOSED') r,
     (SELECT coalesce(sum(pnl), 0) AS unrealized,
             coalesce(sum(abs(qty) * coalesce(mtm_price, entry_price)), 0) AS gross,
             count(*) AS n_open
      FROM paper_trades WHERE status = 'OPEN') u
RETURNING equity, realized_pnl, unrealized_pnl, gross_exposure, n_open
"""


class PaperEngine:
    """asyncpg-backed position engine. All state lives in paper_trades."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool

    @classmethod
    async def create(cls) -> "PaperEngine":
        dsn = (os.getenv("DATABASE_URL") or "").replace(
            "postgresql+asyncpg://", "postgresql://")
        if not dsn:
            raise RuntimeError("DATABASE_URL not set")
        pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4,
                                         timeout=15)
        engine = cls(pool)
        await engine.reconcile_on_boot()
        return engine

    async def close(self) -> None:
        await self._pool.close()

    async def reconcile_on_boot(self) -> None:
        """Crash-safe reconciliation = read the OPEN rows and say so. The DB
        is the ledger; nothing else needs rebuilding."""
        pending = await self._pool.fetch(_PENDING_ROWS_SQL)
        if pending:
            logger.info(f"[BOOT] {len(pending)} PENDING signal(s) awaiting "
                        f"next-open fill")
        rows = await self._pool.fetch(_OPEN_ROWS_SQL)
        logger.info(f"[BOOT] {len(rows)} OPEN paper position(s) carried over")
        for r in rows:
            logger.info(
                f"[BOOT]   #{r['id']} {r['strategy']} {r['side']} "
                f"{r['symbol']} {r['instrument']} x{r['qty']} "
                f"@ {r['entry_price']} since {r['entry_at']}")

    # ── entries ──────────────────────────────────────────────────────────────

    async def open_position(self, intent: SignalIntent, ltp: float | None,
                            notional: float | None = None) -> int | None:
        """Fill at LTP with an adverse slippage haircut. No LTP → REJECTED row
        (auditable, never silent). Returns the new row id, or None."""
        prov = dict(intent.provenance)
        prov["entry_ltp_raw"] = ltp
        prov["slippage_bps"] = PAPER_SLIPPAGE_BPS

        if ltp is None or ltp <= 0:
            logger.info(f"[REJECT] {intent.strategy} {intent.side} "
                        f"{intent.symbol}: no_ltp")
            return await self._insert_rejected(intent, "no_ltp", prov)

        if intent.strategy == "pead_long":
            gap_reject = await self._pead_gap_check(intent.symbol, ltp)
            if gap_reject is not None:
                prov["gap_pct"] = gap_reject
                logger.info(f"[REJECT] pead_long {intent.symbol}: gap_guard "
                            f"(fill {gap_reject:+.2f}% above prev close > "
                            f"{PAPER_PEAD_MAX_GAP_PCT}%)")
                return await self._insert_rejected(intent, "gap_guard", prov)

        slip = PAPER_SLIPPAGE_BPS / 1e4
        entry = ltp * (1 - slip) if intent.side == "SHORT" else ltp * (1 + slip)
        want = (notional or PAPER_NOTIONAL_PER_TRADE) / entry
        # Futures trade in exchange LOT MULTIPLES only (founder requirement
        # 2026-07-30). Minimum one lot — real contract values (5-15L) exceed
        # the per-trade notional, exactly as they do for a real small book.
        # pead_hedge quantizes by lot VALUE in the hedge loop instead.
        if intent.instrument == "FUT" and intent.strategy != "pead_hedge":
            lot = await self._fut_lot_size(intent.symbol)
            if not lot:
                logger.info(f"[REJECT] {intent.strategy} {intent.side} "
                            f"{intent.symbol}: no_lot_size")
                return await self._insert_rejected(intent, "no_lot_size", prov)
            n_lots = max(1, math.floor(want / lot))
            qty = n_lots * lot
            prov["lot_size"] = lot
            prov["n_lots"] = n_lots
            prov["contract_value"] = round(qty * entry)
        else:
            qty = math.floor(want)
        if qty < 1:
            # Price above the whole per-trade notional (rare: MRF-class stock).
            logger.info(f"[REJECT] {intent.strategy} {intent.side} "
                        f"{intent.symbol}: zero_qty at entry {entry:.2f}")
            return await self._insert_rejected(intent, "zero_qty", prov)

        gross = float(await self._pool.fetchval(_GROSS_SQL))
        if gross + qty * entry > PAPER_GROSS_CAP:
            logger.info(f"[REJECT] {intent.strategy} {intent.side} "
                        f"{intent.symbol}: gross_cap "
                        f"({gross:.0f} + {qty * entry:.0f} > {PAPER_GROSS_CAP:.0f})")
            prov["gross_at_reject"] = round(gross)
            return await self._insert_rejected(intent, "gross_cap", prov)

        vol60 = await self._fetch_vol60(intent.symbol)
        row = await self._pool.fetchrow(
            _OPEN_SQL, intent.strategy, intent.impact_id, intent.news_id,
            intent.symbol, intent.side, intent.instrument, qty, entry,
            json.dumps(prov, default=str), vol60)
        if row is None:
            logger.info(
                f"[DUP] {intent.strategy}/{intent.symbol}/"
                f"{intent.impact_id} already recorded — idempotent skip")
            return None
        logger.info(
            f"[OPEN] #{row['id']} {intent.strategy} {intent.side} "
            f"{intent.symbol} {intent.instrument} x{qty} @ {entry:.2f} "
            f"(ltp {ltp:.2f}, slip {PAPER_SLIPPAGE_BPS}bps)")
        return row["id"]

    async def queue_position(self, intent: SignalIntent) -> int | None:
        """Market closed at signal time: queue as PENDING for next-open fill.
        Same idempotency key as open_position, so an off-hours replay is
        equally harmless."""
        prov = dict(intent.provenance)
        prov["queued_off_session"] = True
        row = await self._pool.fetchrow(
            _QUEUE_SQL, intent.strategy, intent.impact_id, intent.news_id,
            intent.symbol, intent.side, intent.instrument,
            json.dumps(prov, default=str))
        if row is None:
            logger.info(f"[DUP] {intent.strategy}/{intent.symbol}/"
                        f"{intent.impact_id} already recorded — idempotent skip")
            return None
        logger.info(f"[QUEUE] #{row['id']} {intent.strategy} {intent.side} "
                    f"{intent.symbol} {intent.instrument} — market closed, "
                    f"fills at next open")
        return row["id"]

    async def pending_symbols(self) -> set[str]:
        rows = await self._pool.fetch(_PENDING_ROWS_SQL)
        return {r["symbol"] for r in rows}

    async def fill_pending(self, ltps: dict[str, float]) -> int:
        """Fill PENDING rows at the first live LTP (adverse slippage, same
        math as immediate fills). No LTP → stays PENDING and retries next
        cycle; a made-up fill price would corrupt the pilot."""
        filled = 0
        for r in await self._pool.fetch(_PENDING_ROWS_SQL):
            ltp = ltps.get(r["symbol"])
            if ltp is None or ltp <= 0:
                continue
            slip = PAPER_SLIPPAGE_BPS / 1e4
            entry = (ltp * (1 - slip) if r["side"] == "SHORT"
                     else ltp * (1 + slip))
            qty = math.floor(PAPER_NOTIONAL_PER_TRADE / entry)
            fill_prov = {"fill_ltp_raw": ltp, "slippage_bps": PAPER_SLIPPAGE_BPS,
                         "filled_from_pending": True}
            if qty < 1:
                await self._pool.execute(
                    _PENDING_REJECT_SQL, r["id"], "zero_qty",
                    json.dumps(fill_prov, default=str))
                logger.info(f"[REJECT] #{r['id']} {r['symbol']}: zero_qty "
                            f"at pending fill {entry:.2f}")
                continue
            if r["strategy"] == "pead_long":
                gap_reject = await self._pead_gap_check(r["symbol"], ltp)
                if gap_reject is not None:
                    await self._pool.execute(
                        _PENDING_REJECT_SQL, r["id"], "gap_guard",
                        json.dumps({**fill_prov, "gap_pct": gap_reject},
                                   default=str))
                    logger.info(f"[REJECT] #{r['id']} {r['symbol']}: "
                                f"gap_guard at pending fill "
                                f"({gap_reject:+.2f}%)")
                    continue
            gross = float(await self._pool.fetchval(_GROSS_SQL))
            if gross + qty * entry > PAPER_GROSS_CAP:
                await self._pool.execute(
                    _PENDING_REJECT_SQL, r["id"], "gross_cap",
                    json.dumps({**fill_prov, "gross_at_reject": round(gross)},
                               default=str))
                logger.info(f"[REJECT] #{r['id']} {r['symbol']}: gross_cap "
                            f"at pending fill")
                continue
            if r["instrument"] == "FUT" and r["strategy"] != "pead_hedge":
                lot = await self._fut_lot_size(r["symbol"])
                if not lot:
                    await self._pool.execute(
                        _PENDING_REJECT_SQL, r["id"], "no_lot_size",
                        json.dumps(fill_prov, default=str))
                    logger.info(f"[REJECT] #{r['id']} {r['symbol']}: "
                                f"no_lot_size at pending fill")
                    continue
                n_lots = max(1, math.floor(qty / lot))
                qty = n_lots * lot
                fill_prov["lot_size"] = lot
                fill_prov["n_lots"] = n_lots
                fill_prov["contract_value"] = round(qty * entry)
            vol60 = await self._fetch_vol60(r["symbol"])
            await self._pool.execute(
                _FILL_SQL, r["id"], qty, entry,
                json.dumps(fill_prov, default=str), vol60)
            filled += 1
            logger.info(f"[FILL] #{r['id']} {r['strategy']} {r['side']} "
                        f"{r['symbol']} x{qty} @ {entry:.2f} "
                        f"(queued {r['created_at']:%H:%M} UTC, ltp {ltp:.2f})")
        return filled

    async def _pead_gap_check(self, symbol: str, ltp: float) -> float | None:
        """Returns the gap % if the fill price exceeds the previous close by
        more than PAPER_PEAD_MAX_GAP_PCT (reject), else None (proceed). No
        prev close available -> proceed (fail-open, paper)."""
        try:
            prev = await self._pool.fetchval(_PREV_CLOSE_SQL, symbol.upper())
        except Exception as e:
            logger.warning(f"gap check failed for {symbol}: {e}")
            return None
        if not prev or prev <= 0:
            return None
        gap_pct = 100.0 * (ltp / float(prev) - 1)
        return round(gap_pct, 2) if gap_pct > PAPER_PEAD_MAX_GAP_PCT else None

    async def _fut_lot_size(self, symbol: str) -> int | None:
        """Exchange lot size for a stock future; None = unknown (caller must
        reject — inventing a quantity would fabricate an untradeable fill)."""
        try:
            from bharat_terminal.papertrade import marks
            lots = await marks.get_fno_lots()
            return lots.get(symbol.upper())
        except Exception as e:
            logger.warning(f"lot lookup failed for {symbol}: {e}")
            return None

    async def _fetch_vol60(self, symbol: str) -> float | None:
        """Trailing-60-session daily vol for stop scaling; None if thin."""
        try:
            row = await self._pool.fetchrow(_VOL60_SQL, symbol.upper())
            v = row["vol60"] if row else None
            return float(v) if v and v > 0 else None
        except Exception as e:
            logger.warning(f"vol60 fetch failed for {symbol}: {e}")
            return None

    async def open_rows(self, strategy: str) -> list:
        """OPEN rows for one strategy (hedge_loop rebalance + close)."""
        return await self._pool.fetch(
            "SELECT id, strategy, symbol, side, instrument, qty, entry_price, "
            "entry_at, trail_extreme, entry_vol60 FROM paper_trades "
            "WHERE status = 'OPEN' AND strategy = $1 ORDER BY id", strategy)

    async def strategy_notional(self, strategy: str) -> float:
        v = await self._pool.fetchval(
            "SELECT coalesce(sum(abs(qty) * entry_price), 0)::float8 "
            "FROM paper_trades WHERE status = 'OPEN' AND strategy = $1",
            strategy)
        return float(v or 0)

    async def record_signal_mark(self, impact_id: str, symbol: str, mark: str,
                                 ltp: float, magnitude: int | None = None,
                                 sentiment: str | None = None) -> None:
        """Absorption-curve exhaust: LTP snapshot for a during-session
        filing signal (t0 at decision, then t5/t15/t30/t60). Idempotent."""
        await self._pool.execute(
            """INSERT INTO paper_signal_marks
                   (impact_id, symbol, magnitude, sentiment, mark, ltp)
               VALUES ($1, $2, $3, $4, $5, $6::float8)
               ON CONFLICT (impact_id, symbol, mark) DO NOTHING""",
            impact_id, symbol, magnitude, sentiment, mark, ltp)

    async def record_rejected(self, intent: SignalIntent,
                              reason: str) -> int | None:
        """Auditable REJECTED row (e.g. stale_event) — same idempotency key."""
        logger.info(f"[REJECT] {intent.strategy} {intent.side} "
                    f"{intent.symbol}: {reason}")
        return await self._insert_rejected(intent, reason,
                                           dict(intent.provenance))

    async def _insert_rejected(self, intent: SignalIntent, reason: str,
                               prov: dict) -> int | None:
        row = await self._pool.fetchrow(
            _REJECT_SQL, intent.strategy, intent.impact_id, intent.news_id,
            intent.symbol, intent.side, intent.instrument, reason,
            json.dumps(prov, default=str))
        return row["id"] if row else None

    # ── marks / exits ────────────────────────────────────────────────────────

    async def open_symbols(self) -> list[str]:
        rows = await self._pool.fetch(
            "SELECT DISTINCT symbol FROM paper_trades WHERE status = 'OPEN'")
        return [r["symbol"] for r in rows]

    async def mark_open_positions(self, ltps: dict[str, float]) -> int:
        """Refresh mtm_price/mtm_at/pnl on OPEN rows that have a live quote."""
        marked = 0
        for r in await self._pool.fetch(
                "SELECT id, symbol FROM paper_trades WHERE status = 'OPEN'"):
            ltp = ltps.get(r["symbol"])
            if ltp:
                await self._pool.execute(_MARK_SQL, r["id"], ltp)
                marked += 1
        return marked

    async def close_position(self, row, price: float, reason: str) -> None:
        res = await self._pool.fetchrow(_CLOSE_SQL, row["id"], price, reason)
        pnl = res["pnl"] if res else None
        logger.info(
            f"[CLOSE] #{row['id']} {row['strategy']} {row['side']} "
            f"{row['symbol']} x{row['qty']} @ {price:.2f} "
            f"reason={reason} pnl={'n/a' if pnl is None else f'{pnl:.0f}'}")

    async def run_exits(self, ltps: dict[str, float]) -> int:
        """Apply exit rules to OPEN rows; returns how many were closed.
        A due exit with no live quote stays open and retries next cycle —
        marking an exit at a made-up price would corrupt the whole pilot."""
        now_ist = datetime.now(IST)
        closed = 0
        for r in await self._pool.fetch(_OPEN_ROWS_SQL):
            ltp = ltps.get(r["symbol"])
            reason = self._exit_reason(r, ltp, now_ist)
            if reason is None:
                continue
            if ltp is None or ltp <= 0:
                logger.warning(
                    f"[EXIT-PENDING] #{r['id']} {r['symbol']} due ({reason}) "
                    f"but no LTP — retrying next cycle")
                continue
            await self.close_position(r, ltp, reason)
            closed += 1
        return closed

    @staticmethod
    def _exit_reason(r, ltp: float | None, now_ist: datetime) -> str | None:
        entry_ist = r["entry_at"].astimezone(IST)
        held_days = (now_ist.date() - entry_ist.date()).days

        # 1. Cash shorts are intraday-only: square off from 15:15 IST; if one
        #    survived overnight (service down at close), flatten at the next
        #    opportunity rather than pretending the rule allows it.
        if r["side"] == "SHORT" and r["instrument"] == "CASH":
            if now_ist.time() >= SQUAREOFF_IST or held_days > 0:
                return "squareoff"

        # 2. Vol-scaled TRAILING stop anchored to the favorable extreme
        #    (founder decision 2026-07-28: wrong classifications must cause
        #    limited, vol-comparable loss; winners keep running). pead_long
        #    is EXEMPT — its validated edge is an unconditional 20d hold.
        #    Flat PAPER_STOP_PCT remains only as the fallback when entry
        #    vol60 was unavailable.
        # pead_hedge exists only to offset the PEAD book — the hedge_loop
        # opens/closes it by rebalance; no stop, no hold, no squareoff.
        if r["strategy"] == "pead_hedge":
            return None
        if ltp and r["entry_price"] and r["strategy"] != "pead_long":
            vol = r["entry_vol60"]
            anchor = r["trail_extreme"] or r["entry_price"]
            if vol and vol > 0:
                dist = PAPER_TRAIL_SIGMA * float(vol)
                if r["side"] == "SHORT" and ltp >= anchor * (1 + dist):
                    return "trail_stop"
                if r["side"] == "LONG" and ltp <= anchor * (1 - dist):
                    return "trail_stop"
            else:
                stop = PAPER_STOP_PCT / 100.0
                if r["side"] == "SHORT" and ltp >= r["entry_price"] * (1 + stop):
                    return "stop"
                if r["side"] == "LONG" and ltp <= r["entry_price"] * (1 - stop):
                    return "stop"

        # 3. Calendar-day holding periods. orderwin_long, like pead_long,
        #    is an unconditional hold (no stop) — the validated D0T stat has
        #    no stop conditioning.
        hold = {"pead_long": PAPER_PEAD_HOLD_DAYS,
                "orderwin_long": PAPER_ORDERWIN_HOLD_DAYS,
                }.get(r["strategy"], PAPER_SHORT_HOLD_DAYS)
        if held_days >= hold:
            return "time_exit"
        return None

    # ── equity curve ─────────────────────────────────────────────────────────

    async def snapshot_equity(self) -> None:
        row = await self._pool.fetchrow(_SNAPSHOT_SQL, PAPER_CAPITAL)
        if row:
            logger.info(
                f"[EQ] equity={row['equity']:.0f} "
                f"realized={row['realized_pnl']:.0f} "
                f"unrealized={row['unrealized_pnl']:.0f} "
                f"gross={row['gross_exposure']:.0f} n_open={row['n_open']}")

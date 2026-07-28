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

# Cash shorts must be flat before close; 15:15 IST leaves a 15-min buffer.
SQUAREOFF_IST = dtime(15, 15)

_OPEN_SQL = """
INSERT INTO paper_trades
    (strategy, impact_id, news_id, symbol, side, instrument, qty,
     entry_price, entry_at, status, provenance)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::float8, now(), 'OPEN', $9::jsonb)
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

_MARK_SQL = """
UPDATE paper_trades
SET mtm_price = $2::float8, mtm_at = now(),
    pnl = (CASE WHEN side = 'LONG' THEN ($2::float8 - entry_price)
                ELSE (entry_price - $2::float8) END) * qty
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
SELECT id, strategy, symbol, side, instrument, qty, entry_price, entry_at
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
        rows = await self._pool.fetch(_OPEN_ROWS_SQL)
        logger.info(f"[BOOT] {len(rows)} OPEN paper position(s) carried over")
        for r in rows:
            logger.info(
                f"[BOOT]   #{r['id']} {r['strategy']} {r['side']} "
                f"{r['symbol']} {r['instrument']} x{r['qty']} "
                f"@ {r['entry_price']} since {r['entry_at']}")

    # ── entries ──────────────────────────────────────────────────────────────

    async def open_position(self, intent: SignalIntent,
                            ltp: float | None) -> int | None:
        """Fill at LTP with an adverse slippage haircut. No LTP → REJECTED row
        (auditable, never silent). Returns the new row id, or None."""
        prov = dict(intent.provenance)
        prov["entry_ltp_raw"] = ltp
        prov["slippage_bps"] = PAPER_SLIPPAGE_BPS

        if ltp is None or ltp <= 0:
            logger.info(f"[REJECT] {intent.strategy} {intent.side} "
                        f"{intent.symbol}: no_ltp")
            return await self._insert_rejected(intent, "no_ltp", prov)

        slip = PAPER_SLIPPAGE_BPS / 1e4
        entry = ltp * (1 - slip) if intent.side == "SHORT" else ltp * (1 + slip)
        qty = math.floor(PAPER_NOTIONAL_PER_TRADE / entry)
        if qty < 1:
            # Price above the whole per-trade notional (rare: MRF-class stock).
            logger.info(f"[REJECT] {intent.strategy} {intent.side} "
                        f"{intent.symbol}: zero_qty at entry {entry:.2f}")
            return await self._insert_rejected(intent, "zero_qty", prov)

        row = await self._pool.fetchrow(
            _OPEN_SQL, intent.strategy, intent.impact_id, intent.news_id,
            intent.symbol, intent.side, intent.instrument, qty, entry,
            json.dumps(prov, default=str))
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

        # 2. Stop on adverse move (shorts; and the symmetric long stop for the
        #    env-gated tailwind branch). pead_long deliberately has NO stop —
        #    the validated PEAD result is an unconditional 20-day hold.
        if ltp and r["entry_price"]:
            stop = PAPER_STOP_PCT / 100.0
            if r["side"] == "SHORT" and ltp >= r["entry_price"] * (1 + stop):
                return "stop"
            if (r["side"] == "LONG" and r["strategy"] == "tailwind_long"
                    and ltp <= r["entry_price"] * (1 - stop)):
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

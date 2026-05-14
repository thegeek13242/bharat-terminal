"""
OI-aware persister for raw F&O ticks.

Strategy: keep per-contract state in memory; persist a row to
`derivative_ticks` only when at least one of these is true:

  • |Δ OI vs last persisted| / last_oi ≥ OI_DELTA_THRESHOLD
  • elapsed since last persisted tick ≥ HEARTBEAT_SECONDS
  • first tick we've ever seen for this contract

Persisted row is stamped with the trigger reason for audit. The in-memory
state has bounded size (one entry per subscribed contract — ~60-100 rows).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Optional, Tuple

import asyncpg

from bharat_terminal.types import DerivativeTick

logger = logging.getLogger(__name__)

DATABASE_URL_RAW = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://bharat:bharat@postgres:5432/bharat_terminal",
)
# asyncpg.connect wants a plain postgresql:// URL — strip the +asyncpg suffix.
DATABASE_URL = DATABASE_URL_RAW.replace("postgresql+asyncpg://", "postgresql://")

OI_DELTA_THRESHOLD = float(os.getenv("FNO_OI_DELTA_THRESHOLD", "0.01"))   # 1%
HEARTBEAT_SECONDS = float(os.getenv("FNO_HEARTBEAT_SECONDS", "60"))
BATCH_FLUSH_SECONDS = float(os.getenv("FNO_BATCH_FLUSH_SECONDS", "5"))
BATCH_FLUSH_MAX = int(os.getenv("FNO_BATCH_FLUSH_MAX", "200"))


class _ContractState:
    __slots__ = ("last_oi", "last_persist_ts", "first_seen")

    def __init__(self) -> None:
        self.last_oi: Optional[int] = None
        self.last_persist_ts: float = 0.0
        self.first_seen: bool = False


class FnoPersister:
    """Filters DerivativeTicks → batches → COPY into derivative_ticks."""

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None
        self._state: Dict[Tuple, _ContractState] = {}   # (symbol, instr, expiry, strike) → state
        self._queue: Deque[Tuple] = deque()
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        self._stats = {"seen": 0, "persisted": 0, "skipped": 0, "errors": 0}

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=4, command_timeout=10
        )
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop(), name="fno-persister-flush")
        logger.info(
            f"[FNO-PERSISTER] connected; thresholds OI_Δ={OI_DELTA_THRESHOLD:.2%}, "
            f"heartbeat={HEARTBEAT_SECONDS:.0f}s, flush={BATCH_FLUSH_SECONDS:.0f}s"
        )

    async def stop(self) -> None:
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_now()
        if self._pool:
            await self._pool.close()
        logger.info(
            f"[FNO-PERSISTER] stopped (seen={self._stats['seen']} "
            f"persisted={self._stats['persisted']} skipped={self._stats['skipped']} "
            f"errors={self._stats['errors']})"
        )

    async def handle(self, tick: DerivativeTick) -> None:
        """Called once per tick by the consumer loop."""
        self._stats["seen"] += 1

        # Resolve persistent key
        try:
            expiry_d = _parse_expiry_to_date(tick.expiry)
        except Exception:
            self._stats["skipped"] += 1
            return
        if expiry_d is None:
            self._stats["skipped"] += 1
            return

        key = (tick.symbol, tick.instrument_type, expiry_d, tick.strike)
        st = self._state.setdefault(key, _ContractState())

        now = time.time()
        reason: Optional[str] = None
        oi = tick.open_interest

        if not st.first_seen:
            reason = "first_seen"
        elif oi is not None and st.last_oi is not None and st.last_oi > 0:
            change = abs(oi - st.last_oi) / max(1, st.last_oi)
            if change >= OI_DELTA_THRESHOLD:
                reason = "oi_delta"
        if reason is None and (now - st.last_persist_ts) >= HEARTBEAT_SECONDS:
            reason = "heartbeat"

        if reason is None:
            self._stats["skipped"] += 1
            return

        st.first_seen = True
        st.last_persist_ts = now
        if oi is not None:
            st.last_oi = oi

        async with self._lock:
            self._queue.append((tick, expiry_d, reason))
            if len(self._queue) >= BATCH_FLUSH_MAX:
                # Trigger an immediate flush — don't wait for the timer
                await self._flush_locked()

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(BATCH_FLUSH_SECONDS)
                async with self._lock:
                    await self._flush_locked()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[FNO-PERSISTER] flush loop error: {e}", exc_info=True)

    async def _flush_now(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._queue or not self._pool:
            return
        batch = list(self._queue)
        self._queue.clear()
        try:
            await self._copy_batch(batch)
            self._stats["persisted"] += len(batch)
        except Exception as e:
            self._stats["errors"] += len(batch)
            logger.error(f"[FNO-PERSISTER] batch insert failed ({len(batch)} rows): {e}")

    async def _copy_batch(self, batch) -> None:
        """Bulk insert via UNNEST — faster than executemany for 100+ rows."""
        if not self._pool:
            return
        # The unique natural index `uq_dt_natural` uses COALESCE(strike, -1)
        # to make NULL-strike (futures) rows comparable. We must spell the
        # ON CONFLICT target identically — Postgres matches the index
        # signature, not column names alone.
        sql = """
            INSERT INTO derivative_ticks (
                timestamp_utc, symbol, instrument_type, expiry, strike, token,
                ltp, open_interest, change_in_oi, volume_day, iv,
                total_buy_qty, total_sell_qty, trigger_reason
            )
            VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11,
                $12, $13, $14
            )
            ON CONFLICT (timestamp_utc, symbol, instrument_type, expiry, COALESCE(strike, -1)) DO NOTHING
        """
        rows = []
        for tick, expiry_d, reason in batch:
            rows.append((
                tick.timestamp_utc.astimezone(timezone.utc),
                tick.symbol,
                tick.instrument_type,
                expiry_d,
                tick.strike,
                tick.token,
                _safe_float(tick.last_price),
                tick.open_interest,
                tick.change_in_oi,
                tick.volume_day,
                _safe_float(tick.iv),
                _safe_int(tick.total_buy_qty),
                _safe_int(tick.total_sell_qty),
                reason,
            ))

        async with self._pool.acquire() as conn:
            await conn.executemany(sql, rows)

        # Periodic stats log
        if self._stats["persisted"] % 200 == 0 and self._stats["persisted"] > 0:
            logger.info(
                f"[FNO-PERSISTER] seen={self._stats['seen']} "
                f"persisted={self._stats['persisted']} "
                f"skipped={self._stats['skipped']} "
                f"err={self._stats['errors']}"
            )


def _parse_expiry_to_date(s):
    """SmartAPI 'DDMMMYYYY' → datetime.date."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d%b%Y").date()
    except (ValueError, TypeError):
        return None


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

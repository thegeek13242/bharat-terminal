"""Paper-trading service entrypoint — internal automated-trading pilot.

WHY a separate service instead of a hook inside analysis: the analysis
pipeline must stay a pure producer of ImpactReports; trading decisions get
their own consumer group ("papertrade") on analysed.impact.in so they can be
restarted, replayed or killed without touching the analysis path, and so a
trading bug can never stall the product pipeline. auto_offset_reset="latest"
is deliberate: on a fresh group there is nothing to catch up on — trading
yesterday's news at today's prices would fabricate an edge.

Structure mirrors analysis/main.py: one consume loop plus background loops
(60s mark-to-market + exits, 300s equity snapshot; both market-hours-only —
LTPs don't move outside the session) and graceful SIGTERM shutdown.
"""
import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from aiokafka import AIOKafkaConsumer

from bharat_terminal.analysis.market_hours import is_market_hours
from bharat_terminal.papertrade import marks
from bharat_terminal.papertrade.engine import PaperEngine
from bharat_terminal.papertrade.rules import (ENABLE_PEAD_LONG, RulesEngine,
                                              SignalIntent)
from bharat_terminal.types import ImpactReport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_ANALYSED = "analysed.impact.in"
CONSUMER_GROUP = "papertrade"

# 5s marks (2026-07-29, founder ask: live PnL on the dashboard) — also
# tightens trailing-stop and squareoff reaction from 60s to 5s. One batched
# kb-service/Angel One quote call per cycle; per-cycle logging is DEBUG
# unless a fill/close happened.
MTM_INTERVAL_S = int(os.getenv("PAPER_MTM_INTERVAL_S", "5"))
SNAPSHOT_INTERVAL_S = 300

# Continuous trading starts 09:15 IST. is_market_hours() opens at 09:00 —
# but 09:00-09:15 is the pre-open auction: quotes are stale/indicative and
# the day's `open` doesn't exist yet. Filling/marking there produced the
# 2026-07-31 phantoms (REFEX filled at yesterday's LTP and "stopped" 60ms
# later on the real −6.7% open; HYUNDAI/BESTAGRO stopped on indicative
# prices). NOTHING trades or marks before this floor.
_SESSION_FLOOR_IST = dtime(9, 15)


def _session_live() -> bool:
    return (is_market_hours()
            and datetime.now(ZoneInfo("Asia/Kolkata")).time()
            >= _SESSION_FLOOR_IST)
# Absorption-curve marks: LTP at decision (t0) then +5/15/30/60 min for
# every DURING-SESSION hop-0 filing impact, traded or not. Measures how
# much of the reaction our ~90s latency can actually reach, per cell.
# Off-session filings are covered by the OHLC gap decomposition instead.
MARK_OFFSETS = ((300, "t5"), (900, "t15"), (1800, "t30"), (3600, "t60"))

# PEAD entry on surprise-row ARRIVAL (S0, 2026-07-29): PIT SUE coverage at
# filing time is 3.7% — the XBRL that produces earnings_surprises lags the
# press-release filing by hours, so the filing-time pead path in rules.py
# almost never fires. The validated drift is 20 DAYS; entering when the
# surprise row lands (hours later) is immaterial to the edge. The filing-
# time path stays enabled for the 3.7% — the NOT EXISTS guard prevents
# double entry.
PEAD_SCAN_INTERVAL_S = int(os.getenv("PAPER_PEAD_SCAN_S", "600"))
PEAD_SCAN_WINDOW_H = int(os.getenv("PAPER_PEAD_SCAN_WINDOW_H", "24"))

# Beta-hedge for the PEAD book (founder decision 2026-07-30): the validated
# PEAD edge is the D10-D1 SPREAD (+2.11% @20d); the long-only leg carries
# the full tape (cohorts: -9% to +6.7% per quarter on beta alone). A
# notional-matched index short converts the paper strategy into the spread
# actually validated. NIFTYBEES prices proxy the NIFTY futures a real
# implementation would use (instrument marked FUT accordingly).
PAPER_HEDGE_ENABLED = os.getenv("PAPER_PEAD_HEDGE", "1") == "1"
PAPER_HEDGE_BAND = float(os.getenv("PAPER_HEDGE_BAND", "100000"))
HEDGE_SYMBOL = os.getenv("PAPER_HEDGE_SYMBOL", "NIFTYBEES")
HEDGE_INTERVAL_S = int(os.getenv("PAPER_HEDGE_INTERVAL_S", "300"))
# Futures lot quantification (founder requirement 2026-07-30): a real NIFTY
# futures hedge trades in whole lots (~lot 75 x ~25,300 = ~19L contract
# value). The hedge notional is quantized to multiples of this value —
# nearest lot; a book below half a lot stays UNHEDGED (logged). Update the
# env as NIFTY drifts; false precision here would be worse than the
# documented approximation.
PAPER_HEDGE_LOT_VALUE = float(os.getenv("PAPER_HEDGE_LOT_VALUE", "1900000"))

_PEAD_SCAN_SQL = """
SELECT es.id, es.symbol, es.sue_composite, es.announced_at, es.period_end
FROM earnings_surprises es
WHERE es.announced_at >= now() - make_interval(hours => $1)
  AND es.sue_composite >= $2
  AND coalesce(es.liquidity_pass, true)
  AND NOT EXISTS (SELECT 1 FROM paper_trades pt
                  WHERE pt.symbol = es.symbol
                    AND pt.strategy = 'pead_long'
                    AND pt.status IN ('PENDING', 'OPEN'))
ORDER BY es.announced_at
"""


async def main():
    engine = await PaperEngine.create()
    rules = RulesEngine(engine.pool, marks.get_fno_universe)

    # Warm the F&O universe before consuming: a cold fetch is a ~35MB download
    # from a slow CDN (up to minutes) and must not happen inside a trade
    # decision. Failure is non-fatal — symbols then default to CASH intraday.
    try:
        fno = await marks.get_fno_universe()
        logger.info(f"F&O universe warmed: {len(fno)} symbols")
    except Exception as e:
        logger.warning(f"F&O universe warm-up failed (non-fatal): {e}")

    consumer = AIOKafkaConsumer(
        TOPIC_ANALYSED,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=CONSUMER_GROUP,
        auto_offset_reset="latest",
        enable_auto_commit=True,
        auto_commit_interval_ms=1000,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    )
    await consumer.start()
    logger.info(f"Consumer connected. Listening on {TOPIC_ANALYSED} "
                f"(group {CONSUMER_GROUP})")

    shutdown_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    # (due_epoch, impact_id, symbol, mark) — in-memory queue for follow-up
    # marks. Deliberately not crash-persistent: t0 rows survive in the DB and
    # a restart only loses the later marks of signals in flight (rare, visible
    # as missing t5+ rows — never miscounted as zero reaction).
    _mark_queue: list[tuple[float, str, str, str]] = []

    async def _capture_t0_marks(report: "ImpactReport"):
        symbols = {ci.symbol.upper() for ci in report.company_impacts
                   if ci.hop_distance == 0 and ci.symbol}
        if not symbols:
            return
        try:
            ltps = await marks.get_ltps(symbols)
        except Exception as e:
            logger.warning(f"[MARKS] t0 LTP fetch failed: {e}")
            return
        by_sym = {ci.symbol.upper(): ci for ci in report.company_impacts
                  if ci.hop_distance == 0}
        now = time.monotonic()
        for sym in symbols:
            ltp = ltps.get(sym)
            if not ltp or ltp <= 0:
                continue
            ci = by_sym[sym]
            await engine.record_signal_mark(
                report.id, sym, "t0", ltp,
                magnitude=ci.magnitude, sentiment=ci.sentiment)
            for offset_s, mark in MARK_OFFSETS:
                _mark_queue.append((now + offset_s, report.id, sym, mark))

    # ── per-report handler ────────────────────────────────────────────────────
    async def handle_report(data: dict):
        try:
            report = ImpactReport(**data)
        except Exception as e:
            logger.error(f"Malformed ImpactReport skipped: {e}")
            return
        if (report.relevant
                and report.news_item.source in ("NSE_FILINGS", "BSE_FILINGS")
                and is_market_hours()):
            try:
                await _capture_t0_marks(report)
            except Exception as e:
                logger.warning(f"[MARKS] t0 capture failed: {e}")

        try:
            intents = await rules.evaluate_report(report)
        except Exception as e:
            logger.error(f"rules.evaluate_report failed for report "
                         f"{data.get('id')}: {e}", exc_info=True)
            return
        if not intents:
            return

        live = [i for i in intents if i.rejected_reason is None]
        market_open = is_market_hours()
        ltps: dict[str, float] = {}
        if live and market_open:
            try:
                ltps = await marks.get_ltps({i.symbol for i in live})
            except Exception as e:
                logger.warning(f"LTP fetch failed — intents fill as no_ltp: {e}")

        for intent in intents:
            try:
                if intent.rejected_reason:
                    # e.g. stale_event: recorded, never silently dropped.
                    await engine.record_rejected(intent, intent.rejected_reason)
                elif not market_open:
                    # Off-session (most results filings land after 15:30 IST):
                    # the pre-news LTP would be a fabricated fill — queue as
                    # PENDING; the MTM loop fills it at the first LTP after
                    # the next open.
                    await engine.queue_position(intent)
                else:
                    await engine.open_position(intent, ltps.get(intent.symbol))
            except Exception as e:
                logger.error(f"engine action failed for {intent.strategy}/"
                             f"{intent.symbol}: {e}", exc_info=True)

    # ── loops ─────────────────────────────────────────────────────────────────
    async def consume_loop():
        async for msg in consumer:
            try:
                await handle_report(msg.value)
            except Exception as e:
                logger.error(f"Failed to process message: {e}", exc_info=True)
            if shutdown_event.is_set():
                break

    async def mtm_loop():
        """Fill queued PENDING signals, then mark OPEN positions and apply
        exit rules (stop / holding period / the >=15:15 IST cash-short
        squareoff) every 60s during market hours."""
        ltp_dry_cycles = 0
        while not shutdown_event.is_set():
            await asyncio.sleep(MTM_INTERVAL_S)
            if not _session_live():
                continue
            try:
                symbols = await engine.open_symbols()
                pending = await engine.pending_symbols()
                # open_symbols() returns a list, pending_symbols() a set —
                # `list | set` crashed every MTM cycle (2026-07-29 hotfix;
                # it blocked the at-open pending fills).
                todo = set(symbols) | set(pending)
                if not todo:
                    continue
                quotes = await marks.get_quotes(todo)
                ltps = {s: q["ltp"] for s, q in quotes.items()}
                # Queued (overnight) signals fill at the day's OPEN — the
                # pre-open auction equilibrium price, straight from Angel One
                # (founder 2026-07-30: a real next-open order executes in the
                # 09:07 auction, not at whatever LTP prints minutes later).
                # After 09:45 IST the auction price is stale — live LTP wins.
                fill_prices = dict(ltps)
                fill_sources: dict[str, str] = {}
                now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
                if pending and now_ist.time() < dtime(9, 45):
                    for s in pending:
                        q = quotes.get(s)
                        if q and q.get("open"):
                            fill_prices[s] = q["open"]
                            fill_sources[s] = "auction_open_angelone"
                # Fail LOUD when the whole book gets zero quotes (2026-07-29:
                # Angel One's expired REST JWT silently returned empty quotes
                # and the at-open fills sat pending for 10 minutes with only
                # INFO logs to show for it). 3 consecutive dry cycles during
                # market hours is an incident, not noise.
                if todo and not ltps:
                    ltp_dry_cycles += 1
                    if ltp_dry_cycles == 3:
                        msg = (f"no LTPs for {len(todo)} tracked symbols for "
                               f"{ltp_dry_cycles} consecutive market-hours "
                               f"cycles — fills/marks/exits are BLOCKED "
                               f"(Angel One session dead?)")
                        logger.error(f"[LTP-DRY] {msg}")
                        try:
                            from bharat_terminal.alerts import get_monitor
                            get_monitor().alert_now("PAPERTRADE_LTP_DRY", msg)
                        except Exception as e:
                            logger.error(f"[LTP-DRY] alert failed: {e}")
                else:
                    ltp_dry_cycles = 0
                filled = (await engine.fill_pending(
                    fill_prices, price_sources=fill_sources)) if pending else 0
                marked = await engine.mark_open_positions(ltps)
                closed = await engine.run_exits(ltps)
                log = logger.info if (filled or closed) else logger.debug
                log(f"[MTM] open={len(symbols)} pending={len(pending)} "
                    f"filled={filled} marked={marked} closed={closed}")
            except Exception as e:
                logger.error(f"[MTM] cycle failed: {e}", exc_info=True)

    async def snapshot_loop():
        while not shutdown_event.is_set():
            await asyncio.sleep(SNAPSHOT_INTERVAL_S)
            if not _session_live():
                continue
            try:
                await engine.snapshot_equity()
            except Exception as e:
                logger.error(f"[EQ] snapshot failed: {e}", exc_info=True)

    async def marks_loop():
        """Fire the due t5/t15/t30/t60 absorption marks every 30s."""
        while not shutdown_event.is_set():
            await asyncio.sleep(30)
            if not _mark_queue:
                continue
            now = time.monotonic()
            due = [m for m in _mark_queue if m[0] <= now]
            if not due:
                continue
            _mark_queue[:] = [m for m in _mark_queue if m[0] > now]
            if not _session_live():
                continue    # due after the close: no valid live price; the
                            # session close is recoverable from OHLC anyway
            try:
                ltps = await marks.get_ltps({d[2] for d in due})
                for _, impact_id, sym, mark in due:
                    ltp = ltps.get(sym)
                    if ltp and ltp > 0:
                        await engine.record_signal_mark(impact_id, sym, mark, ltp)
            except Exception as e:
                logger.error(f"[MARKS] follow-up marks failed: {e}")

    async def pead_scan_loop():
        """Open pead_long positions when qualifying SURPRISE ROWS arrive
        (top-decile trailing SUE + liquidity_pass), independent of the
        filing-time Kafka path. Off-session hits queue as PENDING."""
        while not shutdown_event.is_set():
            await asyncio.sleep(PEAD_SCAN_INTERVAL_S)
            if not ENABLE_PEAD_LONG:
                continue
            try:
                thr = await rules._sue_threshold()
                if thr is None:
                    continue
                rows = await engine.pool.fetch(
                    _PEAD_SCAN_SQL, PEAD_SCAN_WINDOW_H, thr)
                if not rows:
                    continue
                market_open = is_market_hours()
                ltps: dict[str, float] = {}
                if market_open:
                    try:
                        ltps = await marks.get_ltps({r["symbol"] for r in rows})
                    except Exception as e:
                        logger.warning(f"[PEAD-SCAN] LTP fetch failed: {e}")
                for r in rows:
                    intent = SignalIntent(
                        "pead_long", r["symbol"], "LONG", "CASH",
                        f"SUE_{r['id']}", None,
                        provenance={
                            "trigger": "surprise_row",
                            "sue_composite": float(r["sue_composite"]),
                            "sue_threshold": round(float(thr), 3),
                            "surprise_announced_at": str(r["announced_at"]),
                            "period_end": str(r["period_end"]),
                            "gate_flags": {"liquidity_pass": True,
                                           "sue_pass": True},
                        })
                    if market_open:
                        await engine.open_position(
                            intent, ltps.get(r["symbol"]))
                    else:
                        await engine.queue_position(intent)
            except Exception as e:
                logger.error(f"[PEAD-SCAN] cycle failed: {e}", exc_info=True)

    async def hedge_loop():
        """Keep the pead_hedge short's notional matched to the OPEN pead_long
        book within PAPER_HEDGE_BAND. Grows by opening additional shorts,
        shrinks by closing whole hedge rows (newest first); fully unwinds
        when the PEAD book empties."""
        while not shutdown_event.is_set():
            await asyncio.sleep(HEDGE_INTERVAL_S)
            if not PAPER_HEDGE_ENABLED or not is_market_hours():
                continue
            try:
                pead = await engine.strategy_notional("pead_long")
                hedge = await engine.strategy_notional("pead_hedge")
                delta = pead - hedge
                half_lot = PAPER_HEDGE_LOT_VALUE / 2
                tol = max(PAPER_HEDGE_BAND, half_lot)
                if abs(delta) <= tol and not (pead == 0 and hedge > 0):
                    if delta > PAPER_HEDGE_BAND:
                        logger.info(
                            f"[HEDGE] book ₹{delta:.0f} under-hedged but "
                            f"below half a lot (₹{half_lot:.0f}) — staying")
                    continue
                ltps = await marks.get_ltps({HEDGE_SYMBOL})
                ltp = ltps.get(HEDGE_SYMBOL)
                if not ltp or ltp <= 0:
                    logger.warning(f"[HEDGE] no LTP for {HEDGE_SYMBOL}")
                    continue
                if delta > tol:
                    # Quantize to whole NIFTY-futures lots (nearest).
                    n_lots = max(1, int(delta / PAPER_HEDGE_LOT_VALUE + 0.5))
                    lot_notional = n_lots * PAPER_HEDGE_LOT_VALUE
                    intent = SignalIntent(
                        "pead_hedge", HEDGE_SYMBOL, "SHORT", "FUT",
                        f"HEDGE_{int(time.time())}", None,
                        provenance={
                            "trigger": "hedge_rebalance",
                            "pead_notional": round(pead),
                            "hedge_notional_before": round(hedge),
                            "hedge_lots": n_lots,
                            "lot_value": PAPER_HEDGE_LOT_VALUE,
                            "note": "represents a NIFTY futures hedge in "
                                    "whole lots; paper-modeled via "
                                    "NIFTYBEES prices",
                            "gate_flags": {"hedge": True},
                        })
                    await engine.open_position(intent, ltp,
                                               notional=lot_notional)
                    logger.info(f"[HEDGE] extended short by {n_lots} lot(s) "
                                f"≈ ₹{lot_notional:.0f} (pead ₹{pead:.0f})")
                else:
                    # over-hedged (or book empty): close newest rows first
                    excess = hedge - pead
                    for row in reversed(await engine.open_rows("pead_hedge")):
                        if excess <= tol and pead > 0:
                            break
                        await engine.close_position(row, ltp, "hedge_rebal")
                        excess -= abs(row["qty"]) * float(row["entry_price"])
                        if pead == 0 and excess <= 0:
                            break
            except Exception as e:
                logger.error(f"[HEDGE] cycle failed: {e}", exc_info=True)

    consumer_task = asyncio.create_task(consume_loop())
    mtm_task = asyncio.create_task(mtm_loop())
    snapshot_task = asyncio.create_task(snapshot_loop())
    marks_task = asyncio.create_task(marks_loop())
    pead_task = asyncio.create_task(pead_scan_loop())
    hedge_task = asyncio.create_task(hedge_loop())
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, pending = await asyncio.wait(
        [consumer_task, mtm_task, snapshot_task, marks_task, pead_task,
         hedge_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()

    await consumer.stop()
    await marks.close()
    await engine.close()
    logger.info("Paper-trading service stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())

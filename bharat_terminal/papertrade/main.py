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
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiokafka import AIOKafkaConsumer

from bharat_terminal.analysis.market_hours import (SESSION_FLOOR,
                                                   is_session_live,
                                                   is_trading_day)
from bharat_terminal.heartbeat import abeat
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
# Consecutive live-session cycles a tracked symbol may return no quote
# before the one-shot SYMBOL-DRY alert (~5 min at the 5s MTM interval).
PAPER_SYMBOL_DRY_CYCLES = int(os.getenv("PAPER_SYMBOL_DRY_CYCLES", "60"))

# Continuous trading starts 09:15 IST. is_market_hours() opens at 09:00 —
# but 09:00-09:15 is the pre-open auction: quotes are stale/indicative and
# the day's `open` doesn't exist yet. Filling/marking there produced the
# 2026-07-31 phantoms (REFEX filled at yesterday's LTP and "stopped" 60ms
# later on the real −6.7% open; HYUNDAI/BESTAGRO stopped on indicative
# prices). NOTHING trades or marks before this floor. The implementation
# moved to analysis.market_hours (2026-08-15) so the dashboard's manual-exit
# endpoint shares the exact same floor; the historical names stay as aliases.
_SESSION_FLOOR_IST = SESSION_FLOOR


def _session_live(now: datetime | None = None) -> bool:
    """Compat alias for market_hours.is_session_live (moved 2026-08-15)."""
    return is_session_live(now)


# Queued fills need evidence the symbol has actually PRINTED today: Angel
# One's `open` appears only once the day's auction/first trade sets it,
# while `ltp` persists from the previous session. BE/BZ/SM/ST and BSE
# series never enter the pre-open auction, so just after 09:15 their LTP is
# still yesterday's print — the C1 phantom class (REFEX/MINID), which the
# 09:15 floor narrowed but did not close. Before 09:45 the fill IS the
# auction open (a real next-open order executes in the 09:07 auction);
# after that the auction price is stale and the live LTP wins.
_AUCTION_PRICE_CUTOFF = dtime(9, 45)


def select_fill_price(quote: dict | None,
                      now_time: dtime) -> tuple[float, str] | None:
    """(price, source) to fill a queued PENDING row this cycle, or None to
    keep waiting. Pure function so the C1 regression is unit-testable."""
    if not quote:
        return None
    day_open = quote.get("open")
    if not day_open or day_open <= 0:
        return None                     # no print today yet — do not fill
    if now_time < _AUCTION_PRICE_CUTOFF:
        return float(day_open), "auction_open_angelone"
    ltp = quote.get("ltp")
    if ltp and ltp > 0:
        return float(ltp), "live_ltp"
    return None
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

# Pre-open demand confirmation (founder 2026-07-31): the NSE pre-open
# collects orders 09:00-~09:07; by ~09:06 the forming book shows real
# demand/supply per symbol. Overnight signals get CONFIRMED (LONG: demand
# >= supply x ratio; SHORT: inverse) or VETOED before the auction locks —
# an auditable preopen_veto with the full book snapshot preserved so the
# skipped signals' counterfactual outcomes stay measurable. Missing probe
# data (SME/no-preopen names, feed failure) -> proceed (the pre-existing
# validated behavior). Angel One FULL quotes only, per price-data policy.
PAPER_PREOPEN_CONFIRM = os.getenv("PAPER_PREOPEN_CONFIRM", "1") == "1"
PAPER_PREOPEN_MIN_RATIO = float(os.getenv("PAPER_PREOPEN_MIN_RATIO", "1.0"))
_PREOPEN_PROBE_START = dtime(9, 5, 30)
_PREOPEN_PROBE_END = dtime(9, 7, 0)

# Observe-only mode (founder decision 2026-08-15, from the first accuracy
# readout of 141 scored gate decisions, 2026-08-03→08-14): the veto was
# INVERTED for shorts — vetoed headwind shorts would have returned +2.35%
# d1 / +5.68% d5 vs +1.65%/+3.99% for confirmed ones, because the pre-open
# imbalance is largely priced into the auction open itself (confirmed shorts
# gapped −1.18%, vetoed +0.56% — the gap-vs-capturable lesson again). Only
# orderwin_long showed correct discrimination (+2.59% vs −0.96% at 5d,
# n=8 — thin), so only it still ENFORCES its veto. Every other strategy is
# probed and annotated identically — including "would_veto" when the book
# says no — so the hypothetical gate stays measurable at full sample size.
PAPER_PREOPEN_VETO_STRATEGIES = {
    s.strip() for s in os.getenv(
        "PAPER_PREOPEN_VETO_STRATEGIES", "orderwin_long").split(",")
    if s.strip()
}


def _preopen_decision(side: str, strategy: str,
                      ratio: float | None) -> tuple[bool | None, bool]:
    """(confirmed, enforce) for a probed pending signal.

    confirmed: None = indeterminate (no pre-open book, e.g. SME names) —
    always proceeds. enforce: whether a False confirmation actually vetoes
    (strategy in PAPER_PREOPEN_VETO_STRATEGIES) or is observe-only recorded.
    """
    confirmed = None
    if ratio is not None:
        confirmed = (ratio >= PAPER_PREOPEN_MIN_RATIO if side == "LONG"
                     else ratio <= 1.0 / PAPER_PREOPEN_MIN_RATIO)
    return confirmed, strategy in PAPER_PREOPEN_VETO_STRATEGIES

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
    # marks. t0 rows survive in the DB; on restart _rebuild_mark_queue()
    # re-arms the NOT-YET-DUE followups from recent t0 rows (2026-08-15 —
    # the queue previously just died with the process: 28% of t60 marks
    # missing). Past-due marks stay missing rather than firing late and
    # mislabeled — never miscounted as zero reaction.
    _mark_queue: list[tuple[float, str, str, str]] = []

    async def _rebuild_mark_queue():
        """Re-arm future t5/t15/t30/t60 marks for t0 rows younger than the
        largest offset (60 min + grace)."""
        try:
            rows = await engine.pool.fetch("""
                SELECT t0.impact_id, t0.symbol, t0.ts,
                       array_agg(m.mark) AS have
                FROM paper_signal_marks t0
                LEFT JOIN paper_signal_marks m
                  ON m.impact_id = t0.impact_id AND m.symbol = t0.symbol
                 AND m.mark != 't0'
                WHERE t0.mark = 't0'
                  AND t0.ts >= now() - interval '65 minutes'
                GROUP BY 1, 2, 3
            """)
        except Exception as e:
            logger.warning(f"[MARKS] queue rebuild failed (non-fatal): {e}")
            return
        now_mono = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        armed = 0
        for r in rows:
            have = set(r["have"] or ()) - {None}
            for offset_s, mark in MARK_OFFSETS:
                if mark in have:
                    continue
                due_in = (r["ts"] + timedelta(seconds=offset_s)
                          - now_utc).total_seconds()
                if due_in > 0:
                    _mark_queue.append(
                        (now_mono + due_in, r["impact_id"], r["symbol"], mark))
                    armed += 1
        if armed:
            logger.info(f"[MARKS] re-armed {armed} follow-up mark(s) "
                        f"after restart")

    await _rebuild_mark_queue()

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
        # Progressive-delivery events (2026-08-13): "analysing" skeletons carry
        # no impacts, and update_seq>0 re-publishes carry the SAME impacts as
        # the complete event already processed (entries/marks are idempotent by
        # report id anyway — this just skips the redundant LTP fetch + re-eval).
        if data.get("analysis_status", "complete") == "analysing" or data.get("update_seq", 0):
            return
        try:
            report = ImpactReport(**data)
        except Exception as e:
            logger.error(f"Malformed ImpactReport skipped: {e}")
            return
        # _session_live, NOT is_market_hours: a t0 mark taken 09:00-09:15
        # records the pre-open indicative/stale LTP, not a tradeable print —
        # the same phantom class as the 2026-07-31 fills. Pre-floor filings
        # are covered by the OHLC gap decomposition like off-session ones.
        if (report.relevant
                and report.news_item.source in ("NSE_FILINGS", "BSE_FILINGS")
                and _session_live()):
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
        # _session_live, NOT is_market_hours (2026-08-15 fix): a signal
        # arriving 09:00-09:15 used to take the immediate-fill branch at a
        # pre-open indicative LTP — MINID 2026-08-14 filled 09:00:32 at the
        # PREVIOUS close (₹5.13+slip) vs a real auction open of ₹5.19, and it
        # bypassed the pre-open book confirmation entirely. Pre-floor arrivals
        # now queue as PENDING: those before ~09:05:30 get probed by
        # preopen_confirm_loop, and fills happen at the Angel One auction
        # `open` once the session floor lifts (mtm_loop).
        session_live = _session_live()
        quotes: dict[str, dict] = {}
        if live and session_live:
            try:
                quotes = await marks.get_quotes({i.symbol for i in live})
            except Exception as e:
                # 2026-08-15: a transient quote failure used to become a
                # PERMANENT no_ltp reject (idempotency key blocks retries).
                # Queue instead — the MTM loop fills once quotes recover.
                logger.warning(f"quote fetch failed — intents queue for "
                               f"retry: {e}")

        for intent in intents:
            try:
                if intent.rejected_reason:
                    # e.g. stale_event: recorded, never silently dropped.
                    await engine.record_rejected(intent, intent.rejected_reason)
                elif not session_live:
                    # Off-session or pre-open (most results filings land after
                    # 15:30 IST): the pre-news/pre-open LTP would be a
                    # fabricated fill — queue as PENDING; the MTM loop fills it
                    # at the auction open / first LTP after the session floor.
                    await engine.queue_position(intent)
                else:
                    q = quotes.get(intent.symbol)
                    if q and q.get("ltp") and q.get("open"):
                        await engine.open_position(intent, q["ltp"])
                    else:
                        # No quote, or no evidence of a print TODAY (`open`
                        # absent: illiquid BE/SM/ST/BSE names before their
                        # first trade — filling at the carried-over LTP is
                        # the C1 phantom class). Queue for the MTM loop's
                        # print-gated fill instead of rejecting forever.
                        intent.provenance["queued_no_today_print"] = True
                        await engine.queue_position(intent)
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
        squareoff) every cycle during the live session."""
        ltp_dry_cycles = 0
        # Per-symbol quote-miss streaks (2026-08-15): the LTP-DRY alert only
        # fires when the WHOLE book gets zero quotes — a single symbol that
        # stops resolving (suspension, series migration, an upstream symbol
        # error) used to freeze its marks and exits invisibly.
        sym_dry: dict[str, int] = {}
        while not shutdown_event.is_set():
            await asyncio.sleep(MTM_INTERVAL_S)
            # Liveness beat for /admin/health — before the session gate, so
            # the loop reads alive off-hours too. NOTE this only proves the
            # loop is SCHEDULED; hb:papertrade:mtm below proves cycles are
            # actually COMPLETING (stamped after a successful body only).
            await abeat("papertrade")
            if not _session_live():
                continue
            try:
                symbols = await engine.open_symbols()
                pending = await engine.pending_symbols()
                expired = await engine.expire_stale_pending()
                # open_symbols() returns a list, pending_symbols() a set —
                # `list | set` crashed every MTM cycle (2026-07-29 hotfix;
                # it blocked the at-open pending fills).
                todo = set(symbols) | set(pending)
                if not todo:
                    await abeat("papertrade:mtm")
                    continue
                quotes = await marks.get_quotes(todo)
                ltps = {s: q["ltp"] for s, q in quotes.items()}
                # Queued signals fill ONLY with evidence of a print today
                # (select_fill_price: day `open` present → auction open
                # before 09:45, live LTP after; no `open` → wait). The old
                # bare-LTP fallback filled non-auction series at yesterday's
                # price (C1).
                fill_prices: dict[str, float] = {}
                fill_sources: dict[str, str] = {}
                now_t = datetime.now(ZoneInfo("Asia/Kolkata")).time()
                for s in pending:
                    sel = select_fill_price(quotes.get(s), now_t)
                    if sel:
                        fill_prices[s], fill_sources[s] = sel
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
                # Per-symbol starvation: alert once when a tracked symbol has
                # returned no quote for PAPER_SYMBOL_DRY_CYCLES consecutive
                # live-session cycles (~5 min at the 5s interval).
                for s in todo:
                    if s in quotes:
                        sym_dry.pop(s, None)
                        continue
                    sym_dry[s] = sym_dry.get(s, 0) + 1
                    if sym_dry[s] == PAPER_SYMBOL_DRY_CYCLES:
                        msg = (f"{s}: no quote for {PAPER_SYMBOL_DRY_CYCLES} "
                               f"consecutive live-session cycles — its marks/"
                               f"fills/exits are FROZEN (unresolvable symbol? "
                               f"suspension? series migration?)")
                        logger.error(f"[SYMBOL-DRY] {msg}")
                        try:
                            from bharat_terminal.alerts import get_monitor
                            get_monitor().alert_now(
                                "PAPERTRADE_SYMBOL_DRY", msg)
                        except Exception as e:
                            logger.error(f"[SYMBOL-DRY] alert failed: {e}")
                filled = (await engine.fill_pending(
                    fill_prices, price_sources=fill_sources)
                    if fill_prices else 0)
                marked = await engine.mark_open_positions(ltps)
                closed = await engine.run_exits(ltps)
                log = (logger.info if (filled or closed or expired)
                       else logger.debug)
                log(f"[MTM] open={len(symbols)} pending={len(pending)} "
                    f"filled={filled} marked={marked} closed={closed} "
                    f"expired={expired}")
                # Work-stamp: cycles are completing, not merely scheduled.
                # Expires (TTL 180s) when bodies fail — the watchdog's signal.
                await abeat("papertrade:mtm")
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
                # _session_live, NOT is_market_hours (2026-08-15, same fix as
                # handle_report): a scan tick inside 09:00-09:15 would fill
                # pead_long at a pre-open indicative LTP. Queue instead — the
                # gap guard at pending-fill needs the REAL open anyway.
                session_live = _session_live()
                quotes: dict[str, dict] = {}
                if session_live:
                    try:
                        quotes = await marks.get_quotes(
                            {r["symbol"] for r in rows})
                    except Exception as e:
                        logger.warning(f"[PEAD-SCAN] quote fetch failed — "
                                       f"hits queue for retry: {e}")
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
                    q = quotes.get(r["symbol"])
                    if session_live and q and q.get("ltp") and q.get("open"):
                        await engine.open_position(intent, q["ltp"])
                    else:
                        # Off-session, no quote, or no print today (`open`
                        # absent) — queue; the MTM loop fills print-gated.
                        if session_live:
                            intent.provenance["queued_no_today_print"] = True
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
            # _session_live: NFO has no pre-open auction — a hedge adjusted
            # 09:00-09:15 would price at yesterday's futures LTP.
            if not PAPER_HEDGE_ENABLED or not _session_live():
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

    async def preopen_confirm_loop():
        """One shot per trading day inside 09:05:30-09:07:00 IST: probe the
        pre-open book for every queued symbol and confirm or veto."""
        last_run_day = None
        while not shutdown_event.is_set():
            await asyncio.sleep(10)
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
            # is_trading_day, not weekday (2026-08-15): on a weekday NSE
            # holiday the "pre-open book" is the previous session's stale
            # data — a veto decided on it would kill real queued signals.
            if (not PAPER_PREOPEN_CONFIRM
                    or not is_trading_day(now_ist.date())
                    or not (_PREOPEN_PROBE_START <= now_ist.time()
                            <= _PREOPEN_PROBE_END)
                    or last_run_day == now_ist.date()):
                continue
            last_run_day = now_ist.date()
            try:
                rows = await engine.pending_rows()
                if not rows:
                    continue
                quotes = await marks.get_full_quotes(
                    {r["symbol"] for r in rows})
                for r in rows:
                    q = quotes.get(r["symbol"])
                    if (not q or q.get("tot_buy_qty") is None
                            or q.get("tot_sell_qty") is None):
                        await engine.annotate_pending(
                            r["id"], {"preopen_probe": "missing"})
                        logger.info(f"[PREOPEN] #{r['id']} {r['symbol']}: "
                                    f"no book data — proceeding")
                        continue
                    buy, sell = q["tot_buy_qty"], q["tot_sell_qty"]
                    ratio = (min(buy / sell, 999.0) if sell and sell > 0
                             else (999.0 if buy else None))
                    snap = {"preopen_probe": {
                        "at": now_ist.isoformat(),
                        "tot_buy_qty": buy, "tot_sell_qty": sell,
                        "ratio": None if ratio is None else round(ratio, 3),
                        "indicative": q.get("ltp"),
                        "prev_close": q.get("prev_close"),
                        "best_buy": q.get("best_buy"),
                        "best_sell": q.get("best_sell"),
                        "min_ratio": PAPER_PREOPEN_MIN_RATIO,
                    }}
                    confirmed, enforce = _preopen_decision(
                        r["side"], r["strategy"], ratio)
                    snap["preopen_probe"]["veto_enforced"] = enforce
                    if confirmed is False and enforce:
                        await engine.veto_pending(r["id"], "preopen_veto", snap)
                        logger.info(f"[PREOPEN] VETO #{r['id']} {r['side']} "
                                    f"{r['symbol']} ratio={ratio:.2f}")
                    else:
                        snap["preopen_probe"]["confirmed"] = (
                            bool(confirmed) if confirmed is not None
                            else "indeterminate")
                        if confirmed is False:
                            # Observe-only: the book said no, but this
                            # strategy's veto is not enforced — record the
                            # counterfactual and let the signal fill.
                            snap["preopen_probe"]["would_veto"] = True
                            logger.info(
                                f"[PREOPEN] OBSERVE would-veto #{r['id']} "
                                f"{r['side']} {r['symbol']} ratio={ratio:.2f}")
                        else:
                            logger.info(
                                f"[PREOPEN] CONFIRM #{r['id']} {r['side']} "
                                f"{r['symbol']} ratio="
                                f"{'n/a' if ratio is None else f'{ratio:.2f}'}")
                        await engine.annotate_pending(r["id"], snap)
            except Exception as e:
                logger.error(f"[PREOPEN] probe failed: {e}", exc_info=True)

    consumer_task = asyncio.create_task(consume_loop())
    mtm_task = asyncio.create_task(mtm_loop())
    snapshot_task = asyncio.create_task(snapshot_loop())
    marks_task = asyncio.create_task(marks_loop())
    pead_task = asyncio.create_task(pead_scan_loop())
    hedge_task = asyncio.create_task(hedge_loop())
    preopen_task = asyncio.create_task(preopen_confirm_loop())
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, pending = await asyncio.wait(
        [consumer_task, mtm_task, snapshot_task, marks_task, pead_task,
         hedge_task, preopen_task, shutdown_task],
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

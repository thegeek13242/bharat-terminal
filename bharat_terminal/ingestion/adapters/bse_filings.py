"""BSE corporate announcements adapter — every company with a BSE listing.

Polls api.bseindia.com's announcement feed and emits filings for ALL
companies carrying a bse_scrip_code: the BSE-only universe (exchange='BSE',
see kb/sync/bse_sync.py) AND dual-listed NSE names. Dual-listed coverage is
deliberate (2026-07-23): BSE publishes most filings minutes to hours before
NSE (measured live: 2-50 min, including results), so waiting for NSE_FILINGS
meant analyzing after the price had already moved. Nothing is analyzed
twice — companies upload the byte-identical PDF to both exchanges, and the
content-fingerprint dedup layer (ingestion/dedup.py) drops whichever
exchange's copy arrives second.

The feed is market-wide (every BSE listing), so the adapter filters by a
SCRIP_CD → companies.symbol map loaded from Postgres. Cost control is
fail-closed: if that map has never loaded, nothing is emitted — an
unfiltered market-wide feed into the LLM pipeline is the failure mode to
avoid.

`source_name` must be exactly "BSE_FILINGS": stage-1 relevance, the filing
summarizer, and the always-full-pipeline routing all key on it.
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Dict, Optional
from zoneinfo import ZoneInfo

import aiohttp

from bharat_terminal.types import NewsItem
from bharat_terminal.ingestion.pdf_fetch import fetch_pdf_text
from bharat_terminal.ingestion.dedup import text_fingerprint
from .base import BaseAdapter
from .nse_filings import gather_pdf_texts, gather_pdf_texts_as_completed

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

BSE_REFERER = "https://www.bseindia.com/"
ANNOUNCEMENTS_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    "?pageno={page}&strCat=-1&strPrevDate={date}&strScrip=&strSearch=P"
    "&strToDate={date}&strType=C&subcategory=-1"
)
ATTACHMENT_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{name}"

# Hard cap on pages per (day, cycle) — one page = 50 announcements. Steady
# state stops after the first fully-seen page; the cap only bounds cold starts.
MAX_PAGES = int(os.getenv("BSE_ANN_MAX_PAGES", "25"))
SCRIP_MAP_REFRESH_S = 6 * 3600

# Explicit per-request timeout for the announcements API (2026-08-16 wedge
# audit): api.bseindia.com is documented-flaky (empty bodies + read
# timeouts, fine on retry — same behavior the kb backfill walker retries
# around). The session default already bounds requests, but this endpoint
# earns tighter, self-documenting limits: fail fast, let the next cycle
# retry.
_ANN_REQ_TIMEOUT_S = float(os.getenv("BSE_ANN_REQUEST_TIMEOUT_S", "20"))
_ANN_TIMEOUT = aiohttp.ClientTimeout(total=_ANN_REQ_TIMEOUT_S, connect=8,
                                     sock_read=10)


class BSEFilingsAdapter(BaseAdapter):
    source_name = "BSE_FILINGS"
    poll_interval_seconds = 120.0
    # BSE is now the LEADING source for dual-listed names (it publishes
    # minutes before NSE), so market-hours polling matters: 30s halves our
    # added latency vs the old 60s while staying polite on api.bseindia.com
    # (steady state = 2 requests/cycle: today's first fully-seen page stops
    # pagination, plus yesterday's page 1).
    # 2026-08-03 latency analysis: measured detection lag (exchange stamp →
    # analysis consume) was p50 109s. A live probe showed BSE's own API does
    # not list an announcement until ~60s after its stated NEWS_DT, so ~60s
    # is an unfixable BSE-side floor and the remainder is ours. 15s is the
    # rate-limit floor, not a guess: steady state is 2 requests/cycle (today's
    # first fully-seen page + yesterday's page 1) = 8 req/min against
    # rate_limit_per_minute=10. Going to 10s would need 12 req/min and would
    # start getting throttled.
    poll_interval_fast = 15.0
    poll_interval_slow = 300.0
    rate_limit_per_minute = 10

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": BSE_REFERER,
    }

    def __init__(self, redis_client=None):
        super().__init__(redis_client=redis_client)
        self._scrip_map: Dict[str, str] = {}  # SCRIP_CD (str) -> companies.symbol
        self._scrip_map_loaded_at: float = 0.0

    # ── SCRIP_CD → symbol map (the universe filter) ───────────────────────

    async def _refresh_scrip_map(self) -> None:
        """(Re)load every BSE-listed company from Postgres. Keeps the stale
        map on failure; the adapter stays fail-closed until the first
        success. For dual-listed rows the value is the NSE ticker
        (companies.symbol), so downstream symbol joins are exchange-agnostic."""
        if self._scrip_map and (time.time() - self._scrip_map_loaded_at) < SCRIP_MAP_REFRESH_S:
            return
        dsn = (os.getenv("DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
        if not dsn:
            logger.warning(f"[{self.source_name}] DATABASE_URL not set; scrip map unavailable")
            return
        try:
            import asyncpg
            # command_timeout added 2026-08-16 (wedge audit): connect's
            # `timeout` bounds only the handshake — a wedged connection
            # could park conn.fetch() forever with no exception.
            conn = await asyncpg.connect(dsn=dsn, timeout=15,
                                         command_timeout=20)
            try:
                rows = await conn.fetch(
                    "SELECT bse_scrip_code, symbol FROM companies "
                    "WHERE bse_scrip_code IS NOT NULL AND is_active"
                )
            finally:
                await conn.close()
            if rows:
                self._scrip_map = {str(r["bse_scrip_code"]): r["symbol"] for r in rows}
                self._scrip_map_loaded_at = time.time()
                logger.info(
                    f"[{self.source_name}] scrip map loaded: "
                    f"{len(self._scrip_map)} BSE-listed companies (incl. dual-listed)"
                )
            else:
                # Universe not seeded yet — stay fail-closed, retry next refresh window.
                self._scrip_map_loaded_at = time.time() - SCRIP_MAP_REFRESH_S + 300
                logger.warning(f"[{self.source_name}] scrip map query returned 0 rows")
        except Exception as e:
            logger.warning(f"[{self.source_name}] scrip map load failed (keeping previous): {e}")

    # ── Parsing helpers ────────────────────────────────────────────────────

    @staticmethod
    def _parse_news_dt(row: dict, now: datetime) -> datetime:
        """BSE timestamps are naive IST ISO strings ('2026-07-03T17:46:53.547').
        Parse as IST → UTC; clamp future values to now (mirrors NSE adapter)."""
        for field in ("NEWS_DT", "DT_TM", "News_submission_dt"):
            raw = (row.get(field) or "").strip()
            if not raw:
                continue
            try:
                ts = datetime.fromisoformat(raw).replace(tzinfo=IST).astimezone(timezone.utc)
                return min(ts, now)
            except ValueError:
                continue
        return now

    # NEWSSUB embeds the scrip code: "Foo Ltd - 503626 - Subject". Strip it —
    # downstream LLM stages were echoing the number as the company "symbol",
    # which then rendered in the feed instead of the ticker.
    _SCRIP_IN_HEADLINE_RE = re.compile(r"\s*-\s*\d{6}\s*-\s*")

    @classmethod
    def _clean_headline(cls, newssub: str) -> str:
        return cls._SCRIP_IN_HEADLINE_RE.sub(" - ", (newssub or "").strip(), count=1)

    @staticmethod
    def _subcategory(row: dict) -> Optional[str]:
        """The exchange's fine-grained tag, kept VERBATIM (no upper/snake
        mangling like _category does) — it is a controlled vocabulary and
        downstream matches on the exact string."""
        raw = (row.get("SUBCATNAME") or "").strip()
        return raw[:120] or None

    @staticmethod
    def _category(row: dict) -> str:
        raw = (row.get("CATEGORYNAME") or "").strip()
        if not raw or raw == "-":
            return "CORPORATE_ANNOUNCEMENT"
        return raw.upper().replace(" ", "_").replace("/", "_")

    # ── Main fetch loop ────────────────────────────────────────────────────

    async def fetch(self) -> AsyncIterator[NewsItem]:
        await self._refresh_scrip_map()
        if not self._scrip_map:
            # Fail-closed: never emit from an unfiltered market-wide feed.
            return

        session = await self.get_session()
        now_ist = datetime.now(IST)
        # Scan today + yesterday: the second day covers the midnight boundary
        # and restarts; in steady state its first page is fully seen → 1 request.
        days = [now_ist.strftime("%Y%m%d")]
        yesterday = datetime.fromtimestamp(now_ist.timestamp() - 86400, IST)
        days.append(yesterday.strftime("%Y%m%d"))

        for day in days:
            ingest_start = time.time()
            page = 1
            while page <= MAX_PAGES:
                url = ANNOUNCEMENTS_URL.format(page=page, date=day)
                try:
                    async with session.get(url, headers=self.HEADERS,
                                           timeout=_ANN_TIMEOUT) as resp:
                        resp.raise_for_status()
                        raw = await resp.text()
                    if not raw or not raw.strip().startswith("{"):
                        # "No Record Found!" (a bare JSON string) or HTML error page.
                        break
                    data = json.loads(raw)
                    rows = data.get("Table") or []
                except Exception as e:
                    # repr(): asyncio.TimeoutError str()s to '' — the old log
                    # line read "Error fetching 20260803 p1: " with no cause.
                    logger.error(f"[{self.source_name}] Error fetching {day} "
                                 f"p{page}: {type(e).__name__}: {e!r}")
                    self._record_failure()
                    break

                if not rows:
                    break

                # Pass 1: dedupe + filter to our universe.
                pending: list[dict] = []
                unseen = 0
                for row in rows:
                    news_id = (row.get("NEWSID") or "").strip()
                    if not news_id:
                        continue
                    uid = f"BSE_ANN_{news_id}"
                    if self.seen_contains(uid):
                        continue
                    unseen += 1
                    # FILTERED rows (non-universe / no headline) are marked
                    # seen here — no publish obligation, and marking them is
                    # what lets pagination stop at the first fully-seen page.
                    # YIELDABLE rows are NOT marked here: since the 2026-07-20
                    # stall postmortem, base.run() marks them only after the
                    # publish callback succeeds, so a hang means retry — not
                    # silent loss.
                    symbol = self._scrip_map.get(str(row.get("SCRIP_CD") or "").strip())
                    if not symbol:
                        await self.seen_add(uid)
                        continue
                    headline = self._clean_headline(row.get("NEWSSUB") or "")
                    if not headline:
                        await self.seen_add(uid)
                        continue
                    attachment = (row.get("ATTACHMENTNAME") or "").strip()
                    pending.append({
                        "uid": uid,
                        "symbol": symbol,
                        "headline": headline,
                        "ts": self._parse_news_dt(row, datetime.now(timezone.utc)),
                        "category": self._category(row),
                        "subcategory": self._subcategory(row),
                        "attachment": ATTACHMENT_URL.format(name=attachment) if attachment else None,
                        "row": row,
                    })

                # Pass 2: fetch PDF attachments concurrently (bounded,
                # fail-soft) and publish EACH filing the moment its own
                # attachment lands.
                #
                # LATENCY (2026-08-03 analysis): the previous code awaited the
                # whole batch before yielding anything, so on a burst page the
                # first-ready filing waited for the slowest PDF in the batch —
                # pure dead time on the trading path. as_completed drops that
                # barrier; ordering within a cycle no longer matters because
                # downstream dedup/analysis are per-item.
                if pending:
                    async for idx, pdf_text in gather_pdf_texts_as_completed(
                        [p["attachment"] for p in pending],
                        lambda u: fetch_pdf_text(u, session, referer=BSE_REFERER),
                    ):
                        p = pending[idx]
                        # Per-item latency: measured to ITS publish moment.
                        ingest_latency = (time.time() - ingest_start) * 1000
                        row = p["row"]
                        # HEADLINE is BSE's one-line gist (often "Please refer
                        # the enclosed file.") — only a fallback when there is
                        # no PDF text.
                        body = pdf_text or (row.get("HEADLINE") or "").strip() or p["headline"]
                        yield NewsItem(
                            id=p["uid"],
                            source=self.source_name,
                            timestamp_utc=p["ts"],
                            headline=p["headline"],
                            body=body,
                            url=p["attachment"] or (row.get("NSURL") or "").strip()
                            or "https://www.bseindia.com/corporates/ann.html",
                            raw_html=None,
                            ingest_latency_ms=ingest_latency,
                            category=p["category"],
                            subcategory=p["subcategory"],
                            symbols_mentioned=[p["symbol"]],
                            # PDF-derived text only: the fallback fields differ
                            # between exchanges and must never dedup.
                            content_fingerprint=(
                                text_fingerprint(pdf_text) if pdf_text else None
                            ),
                            tier="tier1",
                        )

                if unseen == 0:
                    break  # fully-seen page: everything older is seen too
                total_pages = rows[0].get("TotalPageCnt") or 1
                if page >= int(total_pages):
                    break
                page += 1
                await asyncio.sleep(1.5)  # polite inter-page spacing
